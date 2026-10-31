"""Live CLI progress rendering.

The renderer polls a :class:`ProgressBoard` snapshot on its own timer; it never
runs inside the pipeline.  Rich mode keeps one in-place table, plain mode only
prints lifecycle transitions, and ``off`` prints nothing.  All output goes to
stderr so ``--json`` can keep stdout to exactly one object.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from trackforge.application.dto.progress import (
    CollectionProgressSnapshot,
    ItemProgress,
    ProgressStatus,
)
from trackforge.application.services.progress import ProgressBoard
from trackforge.domain.errors import DomainValidationError

PROGRESS_MODES = ("auto", "rich", "plain", "off")
REPORT_MODES = ("auto", "full", "summary", "off")
DEFAULT_PROGRESS_INTERVAL = 1.0
MIN_PROGRESS_INTERVAL = 0.25
MAX_PROGRESS_INTERVAL = 10.0


def is_dumb_terminal(stream: TextIO | None = None) -> bool:
    target = stream if stream is not None else sys.stderr
    term = os.environ.get("TERM", "").strip().casefold()
    return term == "dumb" or not bool(getattr(target, "isatty", lambda: False)())


def is_ci() -> bool:
    value = os.environ.get("CI", "").strip().casefold()
    return value not in {"", "0", "false", "no", "off"}


def resolve_progress_mode(
    value: str | None,
    *,
    as_json: bool = False,
    stream: TextIO | None = None,
) -> str:
    """Validate ``--progress`` and resolve ``auto`` for the current terminal."""

    mode = (value or "auto").strip().casefold()
    if mode not in PROGRESS_MODES:
        raise DomainValidationError(
            f"unknown progress mode: {value}",
            user_action="Choose auto, rich, plain, or off.",
        )
    if as_json:
        return "off"
    if mode == "auto":
        if is_dumb_terminal(stream):
            return "plain"
        if is_ci():
            return "plain"
        return "rich"
    if mode == "rich" and is_dumb_terminal(stream):
        return "plain"
    return mode


def clamp_progress_interval(value: float | None) -> float:
    """Clamp the render interval to the documented 0.25..10.0 window."""

    if value is None:
        return DEFAULT_PROGRESS_INTERVAL
    try:
        number = float(value)
    except (TypeError, ValueError):
        return DEFAULT_PROGRESS_INTERVAL
    if number != number or number in {float("inf"), float("-inf")}:
        return DEFAULT_PROGRESS_INTERVAL
    return max(MIN_PROGRESS_INTERVAL, min(MAX_PROGRESS_INTERVAL, number))


def resolve_reports_mode(
    value: str | None,
    *,
    as_json: bool = False,
    stream: TextIO | None = None,
) -> str:
    mode = (value or "auto").strip().casefold()
    if mode not in REPORT_MODES:
        raise DomainValidationError(
            f"unknown reports mode: {value}",
            user_action="Choose auto, full, summary, or off.",
        )
    if mode == "auto":
        if as_json:
            return "summary"
        return "summary" if is_dumb_terminal(stream) else "full"
    return mode


def format_elapsed(milliseconds: int) -> str:
    seconds = max(0, int(milliseconds) // 1000)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


class RichProgressRenderer:
    """Build one Rich table representing the latest snapshot."""

    def __init__(self, *, stream: TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stderr

    def render(self, snapshot: CollectionProgressSnapshot) -> Any:
        from rich.table import Table
        from rich.text import Text

        table = Table(
            box=None,
            show_header=False,
            show_edge=False,
            pad_edge=False,
            expand=False,
            padding=(0, 1),
        )
        table.add_column(justify="right", no_wrap=True)
        table.add_column(no_wrap=True)
        table.add_column(no_wrap=True)
        table.add_column(no_wrap=True)
        table.add_column(justify="right", no_wrap=True)
        table.add_column(justify="right", no_wrap=True)
        for item in snapshot.items:
            label = (
                Path(item.output_path).name
                if item.output_path and item.status is ProgressStatus.DONE
                else item.label
            )
            table.add_row(
                Text(f"[{item.position}/{item.total}]"),
                Text(label),
                Text(
                    f"{item.stage} [{item.stage_index}/{item.stage_total}]",
                    style="cyan",
                ),
                Text(item.message or item.status.value, style=_status_style(item)),
                Text(item.percent_text()),
                Text(format_elapsed(item.elapsed_ms)),
            )
        if not snapshot.items:
            table.add_row(
                Text("waiting for items"),
                Text(""),
                Text(""),
                Text(""),
                Text(""),
                Text(""),
            )
        return table


class PlainProgressRenderer:
    """Print one line per lifecycle transition, without ANSI or per-tick spam."""

    def __init__(self, *, stream: TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._seen: dict[int, tuple[ProgressStatus, str]] = {}

    def consume(self, snapshot: CollectionProgressSnapshot) -> None:
        for item in snapshot.items:
            key = (item.status, item.stage)
            previous = self._seen.get(item.position)
            if previous == key:
                continue
            self._seen[item.position] = key
            if previous is None and item.status is ProgressStatus.QUEUED:
                continue
            line = _plain_line(item)
            try:
                print(line, file=self._stream, flush=True)
            except (OSError, ValueError):
                return


def _plain_line(item: ItemProgress) -> str:
    suffix = ""
    if item.status is ProgressStatus.FAILED:
        suffix = f" — {item.message}" if item.message else ""
    elif item.output_path:
        suffix = f" — {item.output_path}"
    return (
        f"[{item.position}/{item.total}] {item.label} — "
        f"{item.status.value} ({item.stage} {item.stage_index}/{item.stage_total}){suffix}"
    )


def _status_style(item: ItemProgress) -> str:
    return {
        ProgressStatus.QUEUED: "dim",
        ProgressStatus.RUNNING: "yellow",
        ProgressStatus.CACHED: "blue",
        ProgressStatus.DONE: "green",
        ProgressStatus.FAILED: "red",
        ProgressStatus.SKIPPED: "magenta",
    }.get(item.status, "")


class ProgressReporter:
    """Background poller that owns the Rich Live/plain render loop."""

    def __init__(
        self,
        board: ProgressBoard,
        *,
        mode: str = "auto",
        interval: float = DEFAULT_PROGRESS_INTERVAL,
        stream: TextIO | None = None,
        scope_id: str | None = None,
        as_json: bool = False,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._board = board
        self._stream = stream if stream is not None else sys.stderr
        self._mode = resolve_progress_mode(mode, as_json=as_json, stream=self._stream)
        self._interval = clamp_progress_interval(interval)
        self._scope_id = scope_id
        self._clock = clock or time.monotonic
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._plain = PlainProgressRenderer(stream=self._stream)
        self._rich = RichProgressRenderer(stream=self._stream)
        self._last_render = 0.0
        self._error: BaseException | None = None

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def interval(self) -> float:
        return self._interval

    @property
    def error(self) -> BaseException | None:
        return self._error

    def _scope(self) -> str | None:
        return self._scope_id or self._board.latest_scope()

    def snapshot(self) -> CollectionProgressSnapshot | None:
        scope = self._scope()
        return None if scope is None else self._board.snapshot(scope)

    def start(self) -> None:
        if self._mode == "off" or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="trackforge-progress",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(1.0, self._interval * 2))
        self._thread = None

    def __enter__(self) -> ProgressReporter:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def _run(self) -> None:
        try:
            if self._mode == "rich":
                self._run_rich()
            elif self._mode == "plain":
                self._run_plain()
        except BaseException as exc:  # a renderer crash must not fail a run
            self._error = exc

    def _run_plain(self) -> None:
        while not self._stop.wait(self._interval):
            snapshot = self.snapshot()
            if snapshot is not None:
                self._plain.consume(snapshot)
        snapshot = self.snapshot()
        if snapshot is not None:
            self._plain.consume(snapshot)

    def _run_rich(self) -> None:
        from rich.console import Console
        from rich.live import Live

        # Let Rich auto-detect the terminal.  ``force_terminal=False`` would
        # disable Live rendering even on a real TTY, which is exactly the
        # "no progress table" failure mode we need to avoid.
        console = Console(file=self._stream, soft_wrap=True)
        snapshot = self.snapshot()
        live = Live(
            self._rich.render(snapshot) if snapshot is not None else self._rich.render(_empty()),
            console=console,
            refresh_per_second=4,
            transient=False,
            auto_refresh=False,
        )
        live.start(refresh=True)
        try:
            while not self._stop.wait(self._interval):
                now = self._clock()
                if now - self._last_render < self._interval:
                    continue
                self._last_render = now
                snapshot = self.snapshot()
                if snapshot is not None:
                    live.update(self._rich.render(snapshot), refresh=True)
            snapshot = self.snapshot()
            if snapshot is not None:
                live.update(self._rich.render(snapshot), refresh=True)
        finally:
            live.stop()


def _empty() -> CollectionProgressSnapshot:
    from datetime import UTC, datetime

    from trackforge.application.dto.progress import CollectionProgressStatus

    now = datetime.now(UTC)
    return CollectionProgressSnapshot(
        collection_id="pending",
        status=CollectionProgressStatus.QUEUED,
        started_at=None,
        updated_at=now,
        completed=0,
        failed=0,
        skipped=0,
        remaining=0,
        items=(),
    )


__all__ = [
    "DEFAULT_PROGRESS_INTERVAL",
    "MAX_PROGRESS_INTERVAL",
    "MIN_PROGRESS_INTERVAL",
    "PROGRESS_MODES",
    "PlainProgressRenderer",
    "ProgressReporter",
    "REPORT_MODES",
    "RichProgressRenderer",
    "clamp_progress_interval",
    "format_elapsed",
    "is_ci",
    "is_dumb_terminal",
    "resolve_progress_mode",
    "resolve_reports_mode",
]
