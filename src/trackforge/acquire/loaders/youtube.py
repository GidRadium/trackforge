"""YouTube site policy on top of the generic yt-dlp loader.

Collections use the two-stage contract from :class:`YtDlpLoader`: a bounded
flat listing first, then a full resolve of a selected item's ``page_url``.
Search arrives in L3.
"""

# pyright: standard

from __future__ import annotations

import re
import urllib.error
import urllib.request
from dataclasses import replace
from urllib.parse import parse_qs, quote, urlsplit

from ..auth import resolve_proxy
from ..config import SourceProfile
from ..errors import SearchNotSupported, UnsupportedPost
from ..models import (
    MediaItem,
    MediaKind,
    Recommendations,
    ResolutionState,
    SearchCandidate,
    SourceInfo,
    SourceRef,
    Thumbnail,
    UrlHint,
)
from .base import LoaderContext, select_best_thumbnail
from .thumbnail_probe import fetch_image_size, parse_image_size
from .ytdlp import YtDlpLoader
from .ytdlp_mapper import (
    best_variant,
    best_video_variant,
    youtube_thumbnail_size,
)

_URL_RE = re.compile(
    r"^https?://(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)(?:/|$)",
    re.IGNORECASE,
)
_YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "www.youtu.be",
    }
)
_VIDEO_PATHS = frozenset({"watch", "shorts", "live", "embed", "v", "clip"})
_THUMBNAIL_FALLBACKS = (
    (
        "https://i.ytimg.com/vi/{id}/maxresdefault.jpg",
        "youtube:maxres",
        1280,
        720,
    ),
    (
        "https://i.ytimg.com/vi/{id}/sddefault.jpg",
        "youtube:sd",
        640,
        480,
    ),
)
_OK_STATUS = range(200, 400)


def _is_youtube_host(host: str) -> bool:
    return host in _YOUTUBE_HOSTS or host.endswith(".youtube.com")


def _is_post_path(segments: list[str]) -> bool:
    """Recognise /post/<id> and channel-scoped /<channel>/post/<id> URLs."""
    if not segments:
        return False
    first = segments[0].lower()
    if first == "post":
        return len(segments) >= 2
    if first in {"channel", "c", "user"}:
        return len(segments) >= 4 and segments[2].lower() == "post"
    if first.startswith("@"):
        return len(segments) >= 3 and segments[1].lower() == "post"
    return False


class YouTubeLoader(YtDlpLoader):
    """Site-specific policy for YouTube single videos."""

    name = "youtube"
    config_key = "youtube"
    priority = 10
    _URL_RE = _URL_RE

    def supports_url(self, url: str) -> bool:
        return bool(_URL_RE.match(url.strip()))

    def classify_url(self, url: str) -> UrlHint:
        try:
            parsed = urlsplit(url.strip())
            host = (parsed.hostname or "").lower()
            query = parse_qs(parsed.query)
        except ValueError:
            return UrlHint.UNKNOWN
        if (
            not host
            or parsed.scheme.lower() not in {"http", "https"}
            or not _is_youtube_host(host)
        ):
            return UrlHint.UNKNOWN
        path = parsed.path.rstrip("/")
        segments = [segment for segment in path.split("/") if segment]
        if not segments:
            return UrlHint.UNKNOWN
        # Short links always identify one video, even when a ``list`` query is
        # attached.  Expanding that list is a user choice (L3).
        if host == "youtu.be" or host.endswith(".youtu.be"):
            return UrlHint.SINGLE
        if _is_post_path(segments):
            return UrlHint.POST
        first = segments[0].lower()
        if first == "playlist":
            return UrlHint.COLLECTION
        if first == "watch":
            if query.get("list", [""])[0].strip():
                return UrlHint.COLLECTION
            if len(segments) == 1:
                return UrlHint.SINGLE
            return UrlHint.UNKNOWN
        if first in _VIDEO_PATHS:
            return UrlHint.SINGLE
        return UrlHint.UNKNOWN

    def get_info(
        self,
        ref: SourceRef,
        *,
        ctx: LoaderContext,
        flat: bool = False,
        limit: int | None = None,
    ) -> SourceInfo:
        # yt-dlp has no extractor for a standalone community post URL; its
        # YoutubeTabIE mistakes ``post`` for a channel and the post id for a
        # tab.  Fail fast with a typed error instead of a misleading network
        # failure.  Actual post extraction belongs to L5.
        if self.classify_url(ref.url) is UrlHint.POST:
            raise UnsupportedPost(
                ref.url,
                message="YouTube community posts are not supported yet",
                hint=(
                    "open the post and send the direct video or playlist link; "
                    "post extraction arrives in L5"
                ),
            )
        return super().get_info(ref, ctx=ctx, flat=flat, limit=limit)

    def choose_thumbnail(
        self,
        item: MediaItem,
        *,
        ctx: LoaderContext,
    ) -> Thumbnail | None:
        candidates = list(item.thumbnails)
        if item.thumbnail is not None:
            candidates.append(item.thumbnail)
        # A flat collection item only has metadata.  Probing maxres/sd here
        # would add network requests to the stage that must stay cheap.
        if item.resolution is ResolutionState.FLAT:
            return select_best_thumbnail(candidates)
        if item.id and item.position in (None, 1):
            existing = {thumbnail.url for thumbnail in candidates}
            for template, source, fallback_width, fallback_height in _THUMBNAIL_FALLBACKS:
                url = template.format(id=quote(item.id, safe=""))
                if url in existing:
                    # yt-dlp already supplied this exact (best) thumbnail.
                    break
                if not self._thumbnail_exists(url, ctx=ctx):
                    continue
                size = youtube_thumbnail_size(url)
                width, height = size or (fallback_width, fallback_height)
                candidates.append(
                    Thumbnail(
                        url=url,
                        width=width,
                        height=height,
                        ext="jpg",
                        source=source,
                    )
                )
                break
        selected = select_best_thumbnail(candidates)
        if selected is None:
            return None
        return self._verified_thumbnail(selected, ctx=ctx)

    def _verified_thumbnail(
        self,
        thumbnail: Thumbnail,
        *,
        ctx: LoaderContext,
    ) -> Thumbnail:
        """Verify the selected thumbnail's real dimensions when possible."""
        try:
            host = urlsplit(thumbnail.url).hostname or ""
        except ValueError:
            return thumbnail
        if not (host == "i.ytimg.com" or host.endswith(".ytimg.com")):
            return thumbnail

        if ctx.http is not None:
            for method_name in ("get_bytes", "get", "read"):
                method = getattr(ctx.http, method_name, None)
                if not callable(method):
                    continue
                try:
                    data = method(thumbnail.url)
                except Exception:  # noqa: BLE001 - optional verification
                    return thumbnail
                if isinstance(data, (bytes, bytearray)):
                    size = parse_image_size(bytes(data))
                    if size is not None:
                        return replace(
                            thumbnail,
                            width=size.width,
                            height=size.height,
                            ext=size.ext,
                            mime=size.mime,
                        )
                return thumbnail
            # A test/injected HTTP object without a body reader: do not hit
            # the real network behind the caller's back.
            return thumbnail

        try:
            proxy = resolve_proxy(
                ctx.profile.proxy,
                label=f"sources.{ctx.profile.name}.proxy",
            )
        except Exception:  # noqa: BLE001 - doctor reports proxy problems
            proxy = None
        size = fetch_image_size(
            thumbnail.url,
            timeout=ctx.profile.socket_timeout,
            proxy=proxy,
            headers=dict(ctx.profile.headers),
        )
        if size is None:
            # The metadata dimensions were not verified, so do not claim them.
            return replace(thumbnail, width=None, height=None)
        return replace(
            thumbnail,
            width=size.width,
            height=size.height,
            ext=size.ext,
            mime=size.mime,
        )

    def recommend(
        self,
        item: MediaItem,
        *,
        ctx: LoaderContext,
    ) -> Recommendations:
        del ctx
        video = best_video_variant(
            item.variants,
            prefer_container="mp4",
            prefer_codec="h264",
        )
        audio = best_variant(
            item.variants,
            kinds=(MediaKind.AUDIO,),
            prefer_container="m4a",
            prefer_codec="aac",
        )
        return Recommendations(
            video=video.id if video is not None else None,
            audio=audio.id if audio is not None else None,
        )

    def search(
        self,
        query: str,
        *,
        limit: int,
        ctx: LoaderContext,
    ) -> list[SearchCandidate]:
        del query, limit, ctx
        raise SearchNotSupported(self.name)

    def _thumbnail_exists(self, url: str, *, ctx: LoaderContext) -> bool:
        http = ctx.http
        if http is not None:
            for method_name in ("exists", "head", "url_exists"):
                method = getattr(http, method_name, None)
                if not callable(method):
                    continue
                try:
                    result = method(url)
                except Exception:  # noqa: BLE001 - missing thumbnail is not fatal
                    return False
                return _status_ok(result)
        return self._head_via_urllib(url, profile=ctx.profile)

    @staticmethod
    def _head_via_urllib(url: str, *, profile: SourceProfile) -> bool:
        headers = {"User-Agent": profile.user_agent or "TrackForge/1.1"}
        for name, value in profile.headers.items():
            headers.setdefault(str(name), str(value))
        try:
            proxy = resolve_proxy(
                profile.proxy,
                label=f"sources.{profile.name}.proxy",
            )
        except Exception:  # noqa: BLE001 - invalid proxy is reported by doctor
            proxy = None
        try:
            request = urllib.request.Request(url, headers=headers, method="HEAD")
            if proxy:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": proxy, "https": proxy})
                )
                response = opener.open(request, timeout=profile.socket_timeout)
            else:
                response = urllib.request.urlopen(
                    request,
                    timeout=profile.socket_timeout,
                )
            try:
                return _status_ok(response)
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        except urllib.error.HTTPError as exc:
            return _status_ok(exc.code)
        except (urllib.error.URLError, OSError, ValueError):
            return False


def _status_ok(result: object) -> bool:
    """Interpret a bool/int/response-like thumbnail probe result."""
    if isinstance(result, bool):
        return result
    if isinstance(result, int):
        return result in _OK_STATUS
    if result is None:
        return False
    status = getattr(result, "status", None)
    if status is None:
        status = getattr(result, "status_code", None)
    if status is None:
        getcode = getattr(result, "getcode", None)
        if callable(getcode):
            try:
                status = getcode()
            except Exception:  # noqa: BLE001
                status = None
    if isinstance(status, int):
        return status in _OK_STATUS
    ok = getattr(result, "ok", None)
    if isinstance(ok, bool):
        return ok
    # A truthy response object without a status is treated as success.
    return bool(result)


__all__ = ["YouTubeLoader"]
