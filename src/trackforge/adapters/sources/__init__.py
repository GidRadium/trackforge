"""Source adapters."""

from __future__ import annotations

from trackforge.adapters.sources.local import LocalFileSource
from trackforge.adapters.sources.local_folder import LocalFolderSource
from trackforge.adapters.sources.ytdlp import (
    YtDlpClient,
    YtDlpPlaylistSource,
    YtDlpSource,
)

__all__ = [
    "LocalFileSource",
    "LocalFolderSource",
    "YtDlpClient",
    "YtDlpPlaylistSource",
    "YtDlpSource",
]
