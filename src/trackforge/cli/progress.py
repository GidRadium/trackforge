"""Progress sinks and the status line for the ``tfg input`` command.

Human output deliberately keeps one line at the bottom of the terminal and
rewrites it in place.  Machine-readable progress is emitted as JSONL on a
separate stream by :class:`JsonlProgressSink`.
"""

# pyright: standard

from __future__ import annotations

import json
import sys
import time
from typing import TextIO

import typer

from trackforge.acquire.progress import ProgressEvent, ProgressStage
from trackforge.acquire.redaction import redact_text


def _is_tty(stream: TextIO) -> bool:
    isatty = getattr(stream, "isatty", None)
    if not callable(isatty):
        return False
    try:
        return bool(isatty())
    except (OSError, ValueError):
        return False


class StatusLine:
    """Rewrite the last terminal line instead of appending progress lines."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream if stream is not None else sys.stderr
        self.is_tty = _is_tty(self.stream)
        self._active = False
        self._last_text = ""

    def update(self, text: str) -> None:
        rendered = redact_text(text)
        if self.is_tty:
            self.stream.write("\r\x1b[2K" + rendered)
            self.stream.flush()
            self._active = True
        elif rendered != self._last_text:
            # Plain logs must not lose progress, but avoid duplicates.
            self.stream.write(rendered + "\n")
            self.stream.flush()
        self._last_text = rendered

    def finish(self, text: str | None = None) -> None:
        if text is not None:
            self.update(text)
        if self.is_tty and self._active:
            self.stream.write("\n")
            self.stream.flush()
        self._active = False
        self._last_text = ""

    def clear(self) -> None:
        if self.is_tty and self._active:
            self.stream.write("\r\x1b[2K")
            self.stream.flush()
        self._active = False
        self._last_text = ""


class JsonlProgressSink:
    """Emit one JSON object per line on stderr."""

    def emit(self, event: ProgressEvent) -> None:
        typer.echo(
            json.dumps(
                progress_event_to_dict(event),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            err=True,
        )


class HumanProgressSink:
    """A single in-place status line with monotonic item progress.

    yt-dlp may download several streams for one item (video then audio).  The
    sink accumulates completed stream totals so the displayed percentage does
    not jump back to 0% when the next stream starts.
    """

    def __init__(self, status: StatusLine | None = None) -> None:
        self.status = status if status is not None else StatusLine()
        self._completed_streams = 0
        self._stream_total: int | None = None
        self._stream_file: str | None = None
        self._stream_downloaded = 0
        self._last_percent = -1
        self._last_bucket = -1
        self._last_update = 0.0
        self._last_stage: ProgressStage | None = None

    def emit(self, event: ProgressEvent) -> None:
        if event.stage is ProgressStage.DOWNLOAD:
            self._download(event)
            return
        if event.stage is ProgressStage.DONE:
            filename = event.filename or ""
            label = f"[done] {filename}".rstrip()
            self.status.finish(self._with_item_prefix(event, label))
            return

        detail = f" {event.message}" if event.message else ""
        label = f"[{event.stage.value}]{detail}"
        self.status.update(self._with_item_prefix(event, label))
        self._last_stage = event.stage

    def _download(self, event: ProgressEvent) -> None:
        now = time.monotonic()
        filename = event.filename or ""
        total = event.total_bytes
        downloaded = event.downloaded_bytes or 0

        if filename != self._stream_file:
            if self._stream_file is not None:
                self._completed_streams += (
                    self._stream_total
                    if self._stream_total is not None
                    else self._stream_downloaded
                )
            self._stream_file = filename
            self._stream_total = total
            self._stream_downloaded = 0
            self._last_percent = -1
            self._last_bucket = -1
        elif downloaded < self._stream_downloaded and self._stream_downloaded > 0:
            # Some extractors report the next stream under the same filename.
            self._completed_streams += (
                self._stream_total
                if self._stream_total is not None
                else self._stream_downloaded
            )
            self._stream_total = total or self._stream_total
            self._last_percent = -1
            self._last_bucket = -1
        elif total is not None:
            self._stream_total = max(self._stream_total or 0, total) or total

        if self._stream_total:
            overall_total = self._completed_streams + self._stream_total
            overall_done = self._completed_streams + downloaded
            percent = min(
                100,
                max(0, int(overall_done * 100 / overall_total)),
            )
            if self._last_percent >= 0:
                # Total estimates can grow mid-stream; never make the visible
                # progress jump backwards.
                percent = max(percent, self._last_percent)
            bucket = percent if self.status.is_tty else percent // 5
            if bucket == self._last_bucket:
                self._stream_downloaded = downloaded
                return
            self._last_bucket = bucket
            self._last_percent = percent
            label = f"[download] {percent:>3}%"
        else:
            if now - self._last_update < 0.2:
                return
            label = f"[download] {downloaded / 1_000_000:.1f} MB"

        self._last_update = now
        self._last_stage = ProgressStage.DOWNLOAD
        self._stream_downloaded = downloaded
        self.status.update(self._with_item_prefix(event, label))

    @staticmethod
    def _with_item_prefix(event: ProgressEvent, label: str) -> str:
        if event.item_count and event.item_count > 1:
            index = event.item_index or 1
            return f"[{index}/{event.item_count}] {label}"
        return label


def progress_event_to_dict(event: ProgressEvent) -> dict[str, object]:
    """Return the stable, secret-free JSONL representation of one event."""
    return {
        "stage": event.stage.value,
        "job_id": event.job_id,
        "item_index": event.item_index,
        "item_count": event.item_count,
        "message": redact_text(event.message) if event.message else None,
        "filename": redact_text(event.filename) if event.filename else None,
        "downloaded_bytes": event.downloaded_bytes,
        "total_bytes": event.total_bytes,
        "speed": event.speed,
        "eta": event.eta,
        "fragment_index": event.fragment_index,
        "fragment_count": event.fragment_count,
    }


__all__ = [
    "HumanProgressSink",
    "JsonlProgressSink",
    "StatusLine",
    "progress_event_to_dict",
]
