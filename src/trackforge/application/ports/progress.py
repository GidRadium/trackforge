"""Progress/board ports consumed by the pipeline and UI adapters.

The sink is deliberately synchronous: implementations must only store the
latest value or hand it to a non-blocking renderer.  A broken UI must never
propagate an exception back into acquisition or analysis.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from trackforge.application.dto.progress import (
    CollectionProgressSnapshot,
    HandlerReport,
    ItemProgress,
)


@runtime_checkable
class ProgressSink(Protocol):
    """Non-blocking receiver for item ticks and finished handler reports."""

    def update(self, item: ItemProgress) -> None: ...

    def handler(self, report: HandlerReport) -> None: ...


@runtime_checkable
class ProgressBoardPort(Protocol):
    """Read/write snapshot store shared by the CLI, API, and future GUI."""

    def start_collection(self, collection_id: str, items: Iterable[ItemProgress]) -> None: ...

    def update(self, item: ItemProgress) -> None: ...

    def handler(self, report: HandlerReport) -> None: ...

    def snapshot(self, collection_id: str) -> CollectionProgressSnapshot: ...

    def reports(self, collection_id: str) -> tuple[HandlerReport, ...]: ...


# The contract name from the plan; the concrete implementation lives in
# ``application.services.progress``.
ProgressBoard = ProgressBoardPort


__all__ = ["ProgressBoard", "ProgressBoardPort", "ProgressSink"]
