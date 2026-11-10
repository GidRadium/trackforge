"""Generic yt-dlp loader for single media items.

The module keeps :mod:`yt_dlp` imports lazy so importing the acquisition
package stays offline.  ``get_info`` only extracts metadata
(``download=False``); downloading belongs to L2.
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
from ..models import DownloadRequest, DownloadResult, SourceInfo, SourceRef
from .base import LoaderCapabilities, LoaderContext, MediaLoader
from .ytdlp_errors import (
    humanize_error,
    is_retryable,
    map_ytdlp_error,
)
from .ytdlp_mapper import (
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
        """Extract metadata only; never download media files."""
        self.preflight(ref.url, ctx.profile)
        info = self._extract(ref.url, ctx=ctx, flat=flat, limit=limit)
        source = YtDlpMapper(ctx.profile).map(info, ref, flat=flat)
        return self._finalize(source, ctx)

    def _extract(
        self,
        url: str,
        *,
        ctx: LoaderContext,
        flat: bool = False,
        limit: int | None = None,
    ) -> Mapping[str, Any]:
        """Run ``extract_info(download=False)`` and sanitize the payload."""
        del flat, limit  # collections are implemented in L1.2
        options = self._base_options(ctx.profile, logger=ctx.logger)
        options.update(
            {
                "skip_download": True,
                "quiet": True,
                "no_warnings": True,
                "noprogress": True,
                "ignoreerrors": False,
                "cachedir": False,
                "extract_flat": False,
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
    "YtDlpLoader",
    "YtDlpMapper",
    "best_audio_variant",
    "best_video_variant",
    "build_variants",
    "build_ydl_options",
    "create_youtube_dl",
    "deno_js_runtimes",
    "humanize_error",
    "is_retryable",
    "map_thumbnails",
    "map_ytdlp_error",
    "parse_partial_date",
    "prepare_ydl_options",
]
