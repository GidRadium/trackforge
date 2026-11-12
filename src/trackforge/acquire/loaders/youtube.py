"""YouTube site policy on top of the generic yt-dlp loader.

Collections use the two-stage contract from :class:`YtDlpLoader`: a bounded
flat listing first, then a full resolve of a selected item's ``page_url``.
Text search is deferred to a later checkpoint.
"""

# pyright: standard

from __future__ import annotations

import re
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

from ..config import SourceProfile
from ..errors import AuthRequired, SearchNotSupported, UnsupportedPost
from ..models import (
    MediaItem,
    MediaKind,
    Recommendations,
    ResolutionState,
    SearchCandidate,
    SourceInfo,
    SourceKind,
    SourceRef,
    Thumbnail,
    UrlHint,
)
from .base import LoaderContext
from .ytdlp import YtDlpLoader
from .ytdlp_mapper import (
    best_variant,
    best_video_variant,
)
from .youtube_thumbnails import youtube_thumbnail_candidates

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
        flat: bool | None = False,
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

    def is_ready(self, profile: SourceProfile) -> bool:
        """YouTube needs an authenticated cookie source."""
        return bool(profile.cookies_file or profile.cookies_from_browser)

    def requires_auth(self, url: str) -> bool:
        try:
            host = (urlsplit(url.strip()).hostname or "").lower()
        except ValueError:
            return False
        return _is_youtube_host(host)

    def unready_error(self, profile: SourceProfile) -> AuthRequired | None:
        """Never fall through to the generic loader for an unconfigured YouTube."""
        if self.is_ready(profile):
            return None
        return AuthRequired(
            "YouTube requires cookies before a network request",
            hint="configure sources.youtube.cookies or use --cookies",
        )

    def thumbnail_candidates(
        self,
        item: MediaItem,
        *,
        ctx: LoaderContext,
    ) -> tuple[Thumbnail, ...]:
        """Return YouTube thumbnail candidates without probing availability."""
        del ctx
        return youtube_thumbnail_candidates(item)

    def _finalize(self, source: SourceInfo, ctx: LoaderContext) -> SourceInfo:
        """Store the full ordered candidate list and the chosen thumbnail."""
        items = []
        for item in source.items:
            candidates = self.thumbnail_candidates(item, ctx=ctx)
            should_fetch = (
                ctx.thumbnail_cache is not None
                and item.resolution is not ResolutionState.FLAT
                and (
                    source.kind is not SourceKind.COLLECTION
                    or item.position in (None, 1)
                )
            )
            if should_fetch:
                chosen = self._resolve_thumbnail(candidates, ctx=ctx)
            else:
                chosen = candidates[0] if candidates else None
            items.append(
                replace(
                    item,
                    thumbnails=candidates,
                    thumbnail=chosen or item.thumbnail,
                    recommendations=self.recommend(item, ctx=ctx),
                )
            )
        thumbnail = next(
            (item.thumbnail for item in items if item.thumbnail is not None),
            None,
        ) or source.thumbnail
        return replace(source, items=tuple(items), thumbnail=thumbnail)

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

__all__ = ["YouTubeLoader"]
