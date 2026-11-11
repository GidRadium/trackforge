"""Progress events, sinks and cancellation."""

# pyright: standard

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from .errors import CancelledError


class ProgressStage(StrEnum):
    """Stages reported by loaders and post-processors."""

    RESOLVE = "resolve"
    SEARCH = "search"
    DOWNLOAD = "download"
    MERGE = "merge"
    EXTRACT_AUDIO = "extract_audio"
    REMUX = "remux"
    CONVERT = "convert"
    SUBTITLES = "subtitles"
    TAG = "tag"
    LYRICS = "lyrics"
    MOVE = "move"
    DONE = "done"


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """One progress observation.

    Unknown byte totals remain ``None`` so consumers can show indeterminate
    progress instead of inventing 100%.
    """

    stage: ProgressStage
    job_id: str
    item_index: int | None = None
    item_count: int | None = None
    message: str | None = None
    filename: str | None = None
    downloaded_bytes: int | None = None
    total_bytes: int | None = None
    speed: float | None = None
    eta: float | None = None
    fragment_index: int | None = None
    fragment_count: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.stage, ProgressStage):
            object.__setattr__(self, "stage", ProgressStage(self.stage))


@runtime_checkable
class ProgressSink(Protocol):
    """Consumer of progress events (CLI, JSON lines, tests)."""

    def emit(self, event: ProgressEvent) -> None:
        """Receive one event. Implementations must not block for long."""
        ...


class NullProgressSink:
    """A sink that deliberately drops every event."""

    def emit(self, event: ProgressEvent) -> None:
        del event


class RecordingSink:
    """A sink that keeps events for assertions and debugging."""

    def __init__(self) -> None:
        self.events: list[ProgressEvent] = []

    def emit(self, event: ProgressEvent) -> None:
        self.events.append(event)

    def clear(self) -> None:
        self.events.clear()

    def __len__(self) -> int:
        return len(self.events)


class CancellationToken:
    """Thread-safe cooperative cancellation."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise CancelledError()


def __getattr__(name: str) -> object:
    """Lazily expose the yt-dlp hook without a module import cycle."""
    if name == "YtDlpProgressHook":
        from .download import YtDlpProgressHook

        return YtDlpProgressHook
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
