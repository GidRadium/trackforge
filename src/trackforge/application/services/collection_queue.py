"""Sequential collection queue with cooperative pause/cancel and pacing.

The queue never runs two items concurrently.  Pause and cancel take effect
after the item currently being processed: the consumer only waits/checks
between items.  Persisted progress remains in ``collection_items``; this
service only coordinates the in-process loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from enum import StrEnum

from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model._validation import ensure_float
from trackforge.domain.model.collection import CollectionItem


class CollectionQueueState(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    CANCELLED = "cancelled"


class CollectionQueue:
    """Yield selected collection items one at a time with min-sleep pacing."""

    def __init__(
        self,
        items: Sequence[CollectionItem],
        *,
        min_sleep: float = 0.0,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._items = tuple(items)
        self._min_sleep = ensure_float(min_sleep, "min_sleep", minimum=0.0)
        self._sleep = sleep or asyncio.sleep
        self._state = CollectionQueueState.RUNNING
        self._wake = asyncio.Event()
        self._wake.set()
        self._yielded = 0
        self._processed = 0
        self._last_index = -1

    @property
    def state(self) -> CollectionQueueState:
        return self._state

    @property
    def cancelled(self) -> bool:
        return self._state is CollectionQueueState.CANCELLED

    @property
    def paused(self) -> bool:
        return self._state is CollectionQueueState.PAUSED

    @property
    def items(self) -> tuple[CollectionItem, ...]:
        return self._items

    @property
    def yielded(self) -> int:
        return self._yielded

    def pause(self) -> None:
        """Ask the consumer to stop before the next item."""

        if self._state is CollectionQueueState.CANCELLED:
            return
        self._state = CollectionQueueState.PAUSED
        self._wake.clear()

    def resume(self) -> None:
        """Resume a paused queue; a cancelled queue cannot be resumed."""

        if self._state is CollectionQueueState.CANCELLED:
            return
        self._state = CollectionQueueState.RUNNING
        self._wake.set()

    def cancel(self) -> None:
        """Request cancellation after the current item."""

        self._state = CollectionQueueState.CANCELLED
        self._wake.set()

    def checkpoint(self) -> bool:
        """Return whether processing may continue at the next item boundary."""

        return self._state is not CollectionQueueState.CANCELLED

    async def __aiter__(self) -> AsyncIterator[CollectionItem]:
        for index, item in enumerate(self._items):
            if not await self._wait_until_ready():
                return
            self._yielded += 1
            self._last_index = index
            yield item
            self._processed += 1
            if index + 1 < len(self._items) and self._min_sleep > 0:
                if not self.cancelled:
                    await self._sleep(self._min_sleep)

    async def _wait_until_ready(self) -> bool:
        while True:
            current = self._current_state()
            if current is CollectionQueueState.CANCELLED:
                return False
            if current is CollectionQueueState.RUNNING:
                return True
            await self._wake.wait()
            current = self._current_state()
            if current is CollectionQueueState.RUNNING:
                return True
            if current is CollectionQueueState.CANCELLED:
                return False

    def _current_state(self) -> CollectionQueueState:
        return self._state

    def __len__(self) -> int:
        return len(self._items)

    def __repr__(self) -> str:
        return (
            f"CollectionQueue(items={len(self._items)}, state={self._state.value!r}, "
            f"yielded={self._yielded})"
        )


def validate_queue_items(items: Sequence[CollectionItem]) -> tuple[CollectionItem, ...]:
    """Validate that queue input is unique and ordered (internal helper)."""

    positions = [item.position for item in items]
    if len(positions) != len(set(positions)):
        raise DomainValidationError("collection queue positions must be unique")
    return tuple(sorted(items, key=lambda item: item.position))


__all__ = [
    "CollectionQueue",
    "CollectionQueueState",
    "validate_queue_items",
]
