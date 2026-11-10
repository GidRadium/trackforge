"""Generic yt-dlp loader — contract only at L0.

The real extraction/mapping arrives in L1.1.  This module deliberately does
not import :mod:`yt_dlp` and never touches the network.
"""

# pyright: standard

from __future__ import annotations

from urllib.parse import urlsplit

from ..models import DownloadRequest, DownloadResult, SourceInfo, SourceRef
from .base import LoaderCapabilities, LoaderContext, MediaLoader


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

    def get_info(
        self,
        ref: SourceRef,
        *,
        ctx: LoaderContext,
        flat: bool = False,
        limit: int | None = None,
    ) -> SourceInfo:
        # TODO(L1.1): call yt-dlp extract_info(download=False), map info_dict.
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
