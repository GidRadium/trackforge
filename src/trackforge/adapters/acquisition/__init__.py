"""Acquisition adapters."""

from __future__ import annotations

from trackforge.adapters.acquisition.local_copy import LocalCopyAcquirer
from trackforge.adapters.acquisition.ytdlp import YtDlpAcquirer

__all__ = ["LocalCopyAcquirer", "YtDlpAcquirer"]
