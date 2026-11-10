"""Generic yt-dlp loader — contract only at L0/L1.0.

The real extraction/mapping arrives in L1.1.  This module deliberately does
not import :mod:`yt_dlp` at import time and never touches the network.  It
does expose the L1.0 option builder and the auth preflight that ``get_info``
will use in L1.1.
"""

# pyright: standard

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..auth import ensure_auth_ready
from ..config import SourceProfile
from ..models import DownloadRequest, DownloadResult, SourceInfo, SourceRef
from .base import LoaderCapabilities, LoaderContext, MediaLoader
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
        # TODO(L1.1): call yt-dlp extract_info(download=False), map info_dict.
        self.preflight(ref.url, ctx.profile)
        del ref, ctx, flat, limit
        raise NotImplementedError("YtDlpLoader.get_info is implemented in L1.1")

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
    "build_ydl_options",
    "create_youtube_dl",
    "deno_js_runtimes",
    "prepare_ydl_options",
]
