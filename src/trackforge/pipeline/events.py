"""In-process progress events.

Events are observability, not the source of truth: run and step state remains
in SQLite and artifacts remain in the artifact store.  The bus keeps a small
per-run history so a late subscriber can still print the sequence, and it
never carries secrets, cookies, or request payloads.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any

from trackforge.domain.model._validation import (
    coerce_enum,
    ensure_datetime,
    require_text,
    utc_now,
)
from trackforge.domain.model.ids import ItemId, RunId, StepId


class EventType(StrEnum):
    """Stable event names used by the CLI and future SSE endpoint."""

    RUN_STARTED = "run.started"
    RUN_PAUSED = "run.paused"
    RUN_FINISHED = "run.finished"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"

    STEP_STARTED = "step.started"
    STEP_PROGRESS = "step.progress"
    STEP_CACHED = "step.cached"
    STEP_FINISHED = "step.finished"
    STEP_FAILED = "step.failed"
    STEP_SKIPPED = "step.skipped"

    @property
    def terminal(self) -> bool:
        return self in _TERMINAL_EVENTS

    @property
    def is_step(self) -> bool:
        return self.value.startswith("step.")


_TERMINAL_EVENTS = frozenset(
    {
        EventType.RUN_FINISHED,
        EventType.RUN_FAILED,
        EventType.RUN_CANCELLED,
    }
)


@dataclass(frozen=True, slots=True)
class Event:
    """One progress event with a monotonically increasing per-bus sequence."""

    type: EventType
    run_id: RunId
    item_id: ItemId | None = None
    step_id: StepId | None = None
    capability: str | None = None
    provider: str | None = None
    duration_ms: int | None = None
    status: str | None = None
    warning_codes: tuple[str, ...] = ()
    sequence: int = 0
    timestamp: datetime = field(default_factory=utc_now)
    message: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "type", coerce_enum(self.type, EventType, "type"))
        object.__setattr__(self, "run_id", require_text(self.run_id, "run_id"))
        if self.item_id is not None:
            object.__setattr__(self, "item_id", require_text(self.item_id, "item_id"))
        if self.step_id is not None:
            object.__setattr__(self, "step_id", require_text(self.step_id, "step_id"))
        for name in ("capability", "provider", "status", "message"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, require_text(value, name))
        if self.duration_ms is not None:
            if isinstance(self.duration_ms, bool) or not isinstance(self.duration_ms, int):
                raise ValueError("duration_ms must be an integer or None")
            if self.duration_ms < 0:
                raise ValueError("duration_ms must be non-negative")
        object.__setattr__(
            self,
            "warning_codes",
            tuple(require_text(code, "warning_code") for code in self.warning_codes),
        )
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
            raise ValueError("sequence must be an integer")
        if self.sequence < 0:
            raise ValueError("sequence must be non-negative")
        object.__setattr__(
            self,
            "timestamp",
            ensure_datetime(self.timestamp, "timestamp"),
        )

    @property
    def event_type(self) -> EventType:
        """Alias that avoids shadowing the built-in ``type`` at call sites."""

        return self.type

    @property
    def is_terminal(self) -> bool:
        return self.type.terminal

    @property
    def warning_codes_text(self) -> str:
        return ",".join(self.warning_codes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type.value,
            "run_id": self.run_id,
            "item_id": self.item_id,
            "step_id": self.step_id,
            "capability": self.capability,
            "provider": self.provider,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "warning_codes": list(self.warning_codes),
            "sequence": self.sequence,
            "timestamp": self.timestamp.isoformat(),
        }


class EventBus:
    """A small fan-out bus with per-run replay and terminal completion."""

    def __init__(self) -> None:
        self._history: dict[str, list[Event]] = {}
        self._subscribers: dict[str, list[asyncio.Queue[Event | None]]] = {}
        self._closed: set[str] = set()
        self._next_sequence = 1

    def publish(self, event: Event) -> Event:
        """Assign a sequence number, record, and fan out ``event``."""

        if not isinstance(event, Event):
            raise TypeError("event must be an Event")
        stored = replace(event, sequence=self._next_sequence)
        self._next_sequence += 1
        run_id = stored.run_id
        self._history.setdefault(run_id, []).append(stored)
        for queue in tuple(self._subscribers.get(run_id, ())):
            queue.put_nowait(stored)
        if stored.is_terminal:
            self._closed.add(run_id)
            for queue in tuple(self._subscribers.get(run_id, ())):
                queue.put_nowait(None)
        return stored

    async def events(self, run_id: RunId, *, replay: bool = True) -> AsyncIterator[Event]:
        """Yield events for one run, stopping after its terminal event.

        Subscribers are registered before the history snapshot, so an event
        published concurrently is delivered exactly once (duplicates from the
        replay snapshot are filtered by sequence number).
        """

        key = require_text(run_id, "run_id")
        queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self._subscribers.setdefault(key, []).append(queue)
        history = tuple(self._history.get(key, ()))
        last_sequence = 0 if replay else (history[-1].sequence if history else 0)
        closed_before_subscribe = key in self._closed
        try:
            if replay:
                for event in history:
                    last_sequence = max(last_sequence, event.sequence)
                    yield event
            if closed_before_subscribe:
                return
            while True:
                item = await queue.get()
                if item is None:
                    return
                if item.sequence <= last_sequence:
                    continue
                last_sequence = item.sequence
                yield item
                if item.is_terminal:
                    return
        finally:
            subscribers = self._subscribers.get(key)
            if subscribers is not None:
                try:
                    subscribers.remove(queue)
                except ValueError:
                    pass
                if not subscribers:
                    self._subscribers.pop(key, None)

    def subscribe(self, run_id: RunId, *, replay: bool = True) -> AsyncIterator[Event]:
        """Alias for :meth:`events`."""

        return self.events(run_id, replay=replay)

    def history(self, run_id: RunId) -> tuple[Event, ...]:
        return tuple(self._history.get(require_text(run_id, "run_id"), ()))

    def is_closed(self, run_id: RunId) -> bool:
        return require_text(run_id, "run_id") in self._closed

    def clear(self, run_id: RunId) -> None:
        """Forget history and close any subscribers for a run."""

        key = require_text(run_id, "run_id")
        self._history.pop(key, None)
        self._closed.add(key)
        for queue in tuple(self._subscribers.pop(key, ())):
            queue.put_nowait(None)


__all__ = ["Event", "EventBus", "EventType"]
