"""Source adapters."""

from __future__ import annotations

from trackforge.adapters.sources.local import LocalFileSource
from trackforge.adapters.sources.ytdlp import YtDlpClient, YtDlpSource

__all__ = ["LocalFileSource", "YtDlpClient", "YtDlpSource"]
