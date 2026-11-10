"""Generic yt-dlp loader for single media items and collections.

The module keeps :mod:`yt_dlp` imports lazy so importing the acquisition
package stays offline.  ``get_info`` only extracts metadata
(``download=False``); downloading belongs to L2.  Collections are resolved in
two stages: a bounded flat listing first, then a full resolve of the selected
item by its ``page_url``.
"""

# pyright: standard

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..auth import ensure_auth_ready
from ..config import SourceProfile
from ..errors import AuthRequired, Unavailable
from ..models import (
    DownloadRequest,
    DownloadResult,
    SourceInfo,
    SourceKind,
    SourceRef,
)
from .base import LoaderCapabilities, LoaderContext, MediaLoader
from .ytdlp_errors import (
    humanize_error,
    is_retryable,
    map_ytdlp_error,
)
from .ytdlp_mapper import (
    DEFAULT_FLAT_LIMIT,
    MAX_FLAT_LIMIT,
    YtDlpMapper,
    best_audio_variant,
    best_video_variant,
    build_variants,
    map_thumbnails,
    parse_partial_date,
)
from .ytdlp_options import (
    build_ydl_options,
    create_youtube_dl,
    deno_js_runtimes,
    prepare_ydl_options,
)

_YT_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "www.youtu.be",
    }
)

_AUTH_AVAILABILITY = frozenset(
    {"private", "premium_only", "subscriber_only", "needs_auth", "members_only"}
)


def _configured_limit(profile: SourceProfile, key: str, default: int) -> int:
    """Read a positive integer limit override from ``SourceProfile.extra``."""
    value = profile.extra.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return default


def effective_flat_limit(
    limit: int | None,
    profile: SourceProfile,
) -> int:
    """Return a bounded item limit for a flat collection extraction.

    ``None``, zero and negative values mean "use the configured default"
    rather than "fetch everything".  Both the default and the hard maximum can
    be overridden for a source through ``extra.flat_limit`` and
    ``extra.flat_limit_max``.
    """
    default = _configured_limit(profile, "flat_limit", DEFAULT_FLAT_LIMIT)
    maximum = _configured_limit(profile, "flat_limit_max", MAX_FLAT_LIMIT)
    requested = (
        limit
        if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0
        else default
    )
    return max(1, min(requested, maximum))


class YtDlpLoader(MediaLoader):
    """Fallback loader for any http(s) URL understood by yt-dlp."""

    name = "yt-dlp"
    config_key = "yt_dlp"
    priority = 1000
    capabilities = LoaderCapabilities(
        search=True,
        playlists=True,
        posts=True,
        live=True,
        subtitles=True,
        cookies=True,
        proxy=True,
    )

    def supports_url(self, url: str) -> bool:
        """Accept any absolute http(s) URL; yt-dlp's GenericIE decides later."""
        try:
            parsed = urlsplit(url.strip())
        except ValueError:
            return False
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    def is_ready(self, profile: SourceProfile) -> bool:
        """YouTube profiles need cookies before a network call.

        The generic ``yt_dlp`` profile stays usable without cookies because
        many non-YouTube sites are public.  A profile explicitly named
        ``youtube`` is expected to have an authenticated cookie source.
        """
        if profile.name == "youtube":
            return bool(profile.cookies_file or profile.cookies_from_browser)
        return True

    def preflight(self, url: str, profile: SourceProfile) -> None:
        """Raise a typed auth error before a YouTube request if unconfigured."""
        if _is_youtube_url(url):
            ensure_auth_ready(
                profile,
                source="youtube",
                cookies_required=True,
            )

    def _base_options(
        self,
        profile: SourceProfile,
        *,
        logger: logging.Logger | None = None,
        cookiefile: str | Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Return options suitable for a captured/mock ``YoutubeDL``."""
        return build_ydl_options(
            profile,
            cookiefile=cookiefile,
            logger=logger,
            env=env,
        )

    def _prepared_options(
        self,
        ctx: LoaderContext,
        *,
        env: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Validate auth and build options using the configured cookie file."""
        return prepare_ydl_options(
            ctx.profile,
            staging=ctx.staging,
            job_id=ctx.job_id,
            logger=ctx.logger,
            env=env,
            cookies_required=ctx.profile.name == "youtube",
        )

    @staticmethod
    def _create_youtube_dl(options: Mapping[str, Any]) -> Any:
        return create_youtube_dl(options)

    def get_info(
        self,
        ref: SourceRef,
        *,
        ctx: LoaderContext,
        flat: bool = False,
        limit: int | None = None,
    ) -> SourceInfo:
        """Extract metadata only; never download media files.

        ``flat=True`` asks yt-dlp for a bounded list of collection entries
        without resolving their formats.  A full resolve is intended for one
        selected item's ``page_url``; ``noplaylist`` keeps a ``watch?v&list``
        URL from expanding into the whole playlist.
        """
        self.preflight(ref.url, ctx.profile)
        effective_limit = effective_flat_limit(limit, ctx.profile)
        info = self._extract(
            ref.url,
            ctx=ctx,
            flat=flat,
            limit=effective_limit,
        )
        source = YtDlpMapper(ctx.profile).map(
            info,
            ref,
            flat=flat,
            limit=effective_limit,
        )
        source = self._finalize(source, ctx)
        return self._validate_collection(source, info)

    def _validate_collection(
        self,
        source: SourceInfo,
        info: Mapping[str, Any],
    ) -> SourceInfo:
        """Turn an empty/private collection into a typed acquisition error."""
        if source.kind is not SourceKind.COLLECTION or source.items:
            return source
        availability = str(info.get("availability") or "").strip().lower()
        if availability in _AUTH_AVAILABILITY:
            raise AuthRequired(
                f"collection is not accessible ({availability}): {source.url}",
            )
        raise Unavailable(
            f"collection is empty or has no accessible items: {source.url}",
        )

    def _extract(
        self,
        url: str,
        *,
        ctx: LoaderContext,
        flat: bool = False,
        limit: int | None = None,
    ) -> Mapping[str, Any]:
        """Run ``extract_info(download=False)`` and sanitize the payload."""
        effective_limit = effective_flat_limit(limit, ctx.profile)
        options = self._base_options(ctx.profile, logger=ctx.logger)
        options.update(
            {
                "skip_download": True,
                "quiet": True,
                "no_warnings": True,
                "noprogress": True,
                "ignoreerrors": False,
                "cachedir": False,
                "extract_flat": "in_playlist" if flat else False,
                "playlist_items": f"1:{effective_limit}",
                # A full resolve targets one item; never expand its playlist.
                "noplaylist": not flat,
                "remote_components": [],
            }
        )
        js_runtimes = deno_js_runtimes()
        if js_runtimes:
            options["js_runtimes"] = js_runtimes

        from yt_dlp.utils import DownloadError, ExtractorError

        ydl = self._create_youtube_dl(options)
        try:
            if hasattr(ydl, "__enter__") and hasattr(ydl, "__exit__"):
                with ydl as active:
                    info = active.extract_info(url, download=False)
                    sanitize = getattr(active, "sanitize_info", None)
                    if callable(sanitize):
                        info = sanitize(info)
            else:
                try:
                    info = ydl.extract_info(url, download=False)
                    sanitize = getattr(ydl, "sanitize_info", None)
                    if callable(sanitize):
                        info = sanitize(info)
                finally:
                    close = getattr(ydl, "close", None)
                    if callable(close):
                        close()
        except (DownloadError, ExtractorError) as exc:
            raise map_ytdlp_error(exc, url=url) from exc

        if info is None:
            raise map_ytdlp_error(
                DownloadError("yt-dlp returned no data"),
                url=url,
            )
        if not isinstance(info, Mapping):
            raise map_ytdlp_error(
                DownloadError("yt-dlp returned an unexpected result"),
                url=url,
            )
        return info

    def _finalize(self, source: SourceInfo, ctx: LoaderContext) -> SourceInfo:
        """Apply source-specific thumbnail and recommendation policy."""
        items = tuple(
            replace(
                item,
                thumbnail=self.choose_thumbnail(item, ctx=ctx) or item.thumbnail,
                recommendations=self.recommend(item, ctx=ctx),
            )
            for item in source.items
        )
        thumbnail = next(
            (item.thumbnail for item in items if item.thumbnail is not None),
            None,
        ) or source.thumbnail
        return replace(source, items=items, thumbnail=thumbnail)

    def download(
        self,
        request: DownloadRequest,
        *,
        ctx: LoaderContext,
    ) -> DownloadResult:
        # TODO(L2): plan selections, call yt-dlp extract_info(download=True).
        del request, ctx
        raise NotImplementedError("YtDlpLoader.download is implemented in L2")


def _is_youtube_url(url: str) -> bool:
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if host == "youtu.be" or host.endswith(".youtu.be"):
        return True
    return host in _YT_HOSTS or host.endswith(".youtube.com")


__all__ = [
    "DEFAULT_FLAT_LIMIT",
    "MAX_FLAT_LIMIT",
    "YtDlpLoader",
    "YtDlpMapper",
    "best_audio_variant",
    "best_video_variant",
    "build_variants",
    "build_ydl_options",
    "create_youtube_dl",
    "deno_js_runtimes",
    "effective_flat_limit",
    "humanize_error",
    "is_retryable",
    "map_thumbnails",
    "map_ytdlp_error",
    "parse_partial_date",
    "prepare_ydl_options",
]
