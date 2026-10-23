"""yt-dlp URL source and downloader adapter."""

from trackforge.adapters.sources.ytdlp.client import (
    DownloadedFile,
    DownloadOptions,
    DownloadProgress,
    DumpOptions,
    DumpProgress,
    YtDlpClient,
)
from trackforge.adapters.sources.ytdlp.cookies import (
    CookieSelection,
    cookie_file_expired,
    cookie_file_matches,
    cookie_permissions_ok,
    select_cookie_file,
)
from trackforge.adapters.sources.ytdlp.errors import (
    classify_error,
    is_retryable,
    map_error,
    map_ytdlp_error,
    redact_stderr,
)
from trackforge.adapters.sources.ytdlp.metadata import (
    SourceMetadataObserver,
    build_source_metadata,
    normalize_source_metadata,
    sanitize_snapshot,
)
from trackforge.adapters.sources.ytdlp.source import (
    YtDlpSource,
    fallback_external_id,
    select_best_audio,
)

__all__ = [
    "CookieSelection",
    "DownloadOptions",
    "DownloadProgress",
    "DownloadedFile",
    "DumpOptions",
    "DumpProgress",
    "SourceMetadataObserver",
    "YtDlpClient",
    "YtDlpSource",
    "build_source_metadata",
    "classify_error",
    "cookie_file_expired",
    "cookie_file_matches",
    "cookie_permissions_ok",
    "fallback_external_id",
    "is_retryable",
    "map_error",
    "map_ytdlp_error",
    "normalize_source_metadata",
    "redact_stderr",
    "sanitize_snapshot",
    "select_best_audio",
    "select_cookie_file",
]
