"""In-memory progress board and the per-item tracker used by use cases.

The board stores only the latest item state and a bounded list of handler
reports.  It performs no I/O and never raises into the pipeline; renderers poll
``snapshot()`` on their own cadence.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from trackforge.application.dto.progress import (
    STAGE_INDEX,
    CollectionProgressSnapshot,
    CollectionProgressStatus,
    HandlerReport,
    HandlerStatus,
    ItemProgress,
    ProgressContext,
    ProgressStatus,
    sanitize_text,
)
from trackforge.application.ports.progress import ProgressSink
from trackforge.domain.model._validation import utc_now


@dataclass
class _Scope:
    items: dict[int, ItemProgress] = field(default_factory=dict)
    reports: list[HandlerReport] = field(default_factory=list)
    started_at: datetime | None = None
    updated_at: datetime = field(default_factory=utc_now)
    forced_status: CollectionProgressStatus | None = None


class ProgressBoard:
    """Thread-safe latest-state board keyed by collection/scope id."""

    def __init__(self) -> None:
        self._scopes: dict[str, _Scope] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()

    # -- ProgressBoard contract -----------------------------------------

    def start_collection(self, collection_id: str, items: Iterable[ItemProgress]) -> None:
        key = str(collection_id)
        now = utc_now()
        with self._lock:
            scope = _Scope(started_at=now, updated_at=now)
            for item in items:
                scope.items[int(item.position)] = (
                    item if item.collection_id == key else replace(item, collection_id=key)
                )
                scope.updated_at = max(scope.updated_at, item.updated_at)
            self._scopes[key] = scope
            if key in self._order:
                self._order.remove(key)
            self._order.append(key)

    def update(self, item: ItemProgress) -> None:
        try:
            key = item.collection_id
            with self._lock:
                scope = self._scopes.get(key)
                if scope is None:
                    scope = _Scope(started_at=item.updated_at)
                    self._scopes[key] = scope
                    self._order.append(key)
                scope.items[int(item.position)] = item
                scope.updated_at = max(scope.updated_at, item.updated_at)
                if scope.started_at is None:
                    scope.started_at = item.updated_at
        except Exception:
            # Observability must never break the pipeline.
            return

    def handler(self, report: HandlerReport) -> None:
        try:
            key = report.collection_id or report.scope_id
            with self._lock:
                scope = self._scopes.get(key)
                if scope is None:
                    scope = _Scope(started_at=report.started_at)
                    self._scopes[key] = scope
                    self._order.append(key)
                for index, existing in enumerate(scope.reports):
                    if (
                        existing.position == report.position
                        and existing.item_id == report.item_id
                        and existing.capability_id == report.capability_id
                        and existing.stage == report.stage
                    ):
                        scope.reports[index] = report
                        break
                else:
                    scope.reports.append(report)
                scope.updated_at = max(scope.updated_at, report.started_at)
        except Exception:
            return

    def snapshot(self, collection_id: str) -> CollectionProgressSnapshot:
        key = str(collection_id)
        with self._lock:
            scope = self._scopes.get(key)
            if scope is None:
                now = utc_now()
                return CollectionProgressSnapshot(
                    collection_id=key,
                    status=CollectionProgressStatus.QUEUED,
                    started_at=None,
                    updated_at=now,
                    completed=0,
                    failed=0,
                    skipped=0,
                    remaining=0,
                    items=(),
                )
            items = tuple(sorted(scope.items.values(), key=lambda item: item.position))
            started_at = scope.started_at
            updated_at = scope.updated_at
            forced = scope.forced_status
        completed = sum(
            1 for item in items if item.status in {ProgressStatus.DONE, ProgressStatus.CACHED}
        )
        failed = sum(1 for item in items if item.status is ProgressStatus.FAILED)
        skipped = sum(1 for item in items if item.status is ProgressStatus.SKIPPED)
        remaining = sum(1 for item in items if not item.terminal)
        if forced is not None:
            status = forced
        elif items and remaining == 0:
            if failed and completed:
                status = CollectionProgressStatus.PARTIAL
            elif failed:
                status = CollectionProgressStatus.FAILED
            else:
                status = CollectionProgressStatus.COMPLETED
        elif items and all(item.status is ProgressStatus.QUEUED for item in items):
            status = CollectionProgressStatus.QUEUED
        elif any(item.status is ProgressStatus.RUNNING for item in items) or (
            started_at is not None and remaining
        ):
            status = CollectionProgressStatus.RUNNING
        else:
            status = CollectionProgressStatus.QUEUED
        return CollectionProgressSnapshot(
            collection_id=key,
            status=status,
            started_at=started_at,
            updated_at=updated_at,
            completed=completed,
            failed=failed,
            skipped=skipped,
            remaining=remaining,
            items=items,
        )

    def reports(self, collection_id: str) -> tuple[HandlerReport, ...]:
        with self._lock:
            scope = self._scopes.get(str(collection_id))
            return tuple(scope.reports) if scope is not None else ()

    # -- Extra helpers for CLI/API consumers ---------------------------

    def latest_scope(self) -> str | None:
        with self._lock:
            return self._order[-1] if self._order else None

    def mark_collection(
        self,
        collection_id: str,
        status: CollectionProgressStatus,
    ) -> None:
        try:
            with self._lock:
                scope = self._scopes.get(str(collection_id))
                if scope is not None:
                    scope.forced_status = status
                    scope.updated_at = utc_now()
        except Exception:
            return

    def clear(self, collection_id: str) -> None:
        with self._lock:
            key = str(collection_id)
            self._scopes.pop(key, None)
            if key in self._order:
                self._order.remove(key)

    def __len__(self) -> int:
        with self._lock:
            return len(self._scopes)


class ProgressTracker:
    """Bind a board/sink to one item's collection context.

    ProcessItem calls :meth:`stage` around high-level stages, while the
    executor and acquisition adapters call :meth:`update`/:meth:`percent` for
    measured ticks.  Every call is guarded so a UI failure cannot abort work.
    """

    def __init__(self, sink: ProgressSink, context: ProgressContext) -> None:
        self._sink = sink
        self.context = context
        self._started_at = time.monotonic()
        self._current_stage = context.stage
        self._warnings: list[str] = []

    @property
    def current_stage(self) -> str:
        return self._current_stage

    def bind(self, *, item_id: object | None = None, label: str | None = None) -> None:
        """Attach the acquired item id / final label without restarting the run."""

        changes: dict[str, Any] = {}
        if item_id is not None:
            changes["item_id"] = str(item_id)
        if label is not None:
            text = sanitize_text(label, limit=80)
            if text:
                changes["label"] = text
        if not changes:
            return
        self.context = replace(self.context, **changes)
        self._send(self._item(status=ProgressStatus.QUEUED))

    @property
    def current_index(self) -> int:
        return STAGE_INDEX[self._current_stage]

    def _elapsed_ms(self) -> int:
        return max(0, int((time.monotonic() - self._started_at) * 1000))

    def _item(
        self,
        *,
        stage: str | None = None,
        status: ProgressStatus = ProgressStatus.RUNNING,
        percent: float | None = None,
        message: str | None = None,
        output_path: str | None = None,
        warnings: Sequence[str] | None = None,
        elapsed_ms: int | None = None,
    ) -> ItemProgress:
        target_stage = (stage or self._current_stage).strip().casefold()
        if target_stage not in STAGE_INDEX:
            target_stage = self._current_stage
        if status.terminal:
            percent = None
        elif percent is not None:
            try:
                value = float(percent)
            except (TypeError, ValueError):
                value = None
            if value is None or value != value:
                percent = None
            else:
                percent = max(0.0, min(1.0, value))
        sanitized_message = sanitize_text(message, limit=200) if message else None
        return ItemProgress(
            scope_id=self.context.scope_id,
            collection_id=self.context.collection_id,
            item_id=self.context.item_id,
            position=self.context.position,
            total=self.context.total,
            label=self.context.label,
            stage=target_stage,
            stage_index=STAGE_INDEX[target_stage],
            status=status,
            percent=percent,
            message=sanitized_message,
            elapsed_ms=self._elapsed_ms() if elapsed_ms is None else elapsed_ms,
            output_path=output_path,
            warnings=tuple(warnings if warnings is not None else self._warnings),
        )

    def _send(self, item: ItemProgress) -> None:
        try:
            self._sink.update(item)
        except Exception:
            return

    def update(self, item: ItemProgress) -> None:
        self._send(item)

    def stage(
        self,
        stage: str,
        *,
        status: ProgressStatus = ProgressStatus.RUNNING,
        percent: float | None = None,
        message: str | None = None,
        output_path: str | None = None,
        warnings: Sequence[str] | None = None,
        elapsed_ms: int | None = None,
    ) -> ItemProgress:
        normalized = stage.strip().casefold()
        if normalized not in STAGE_INDEX:
            raise ValueError(f"unknown public stage: {stage!r}")
        self._current_stage = normalized
        self.context = replace(
            self.context,
            stage=normalized,
            stage_index=STAGE_INDEX[normalized],
        )
        item = self._item(
            stage=normalized,
            status=status,
            percent=percent,
            message=message,
            output_path=output_path,
            warnings=warnings,
            elapsed_ms=elapsed_ms,
        )
        self._send(item)
        return item

    def percent(self, fraction: float | None, message: str | None = None) -> None:
        if fraction is None:
            self.stage(self._current_stage, message=message)
            return
        self.stage(self._current_stage, percent=fraction, message=message)

    def done(self, output_path: str | None = None, message: str | None = None) -> None:
        self.stage(
            self._current_stage,
            status=ProgressStatus.DONE,
            message=message,
            output_path=output_path,
        )

    def cached(self, output_path: str | None = None, message: str | None = None) -> None:
        self.stage(
            self._current_stage,
            status=ProgressStatus.CACHED,
            message=message,
            output_path=output_path,
        )

    def failed(self, message: str | None = None) -> None:
        self.stage(self._current_stage, status=ProgressStatus.FAILED, message=message)

    def skipped(self, message: str | None = None) -> None:
        self.stage(self._current_stage, status=ProgressStatus.SKIPPED, message=message)

    def add_warning(self, warning: str) -> None:
        text = sanitize_text(warning, limit=200)
        if text and text not in self._warnings:
            self._warnings.append(text)

    def handler(self, report: HandlerReport) -> None:
        try:
            self._sink.handler(report)
        except Exception:
            return

    def report(
        self,
        *,
        capability_id: str,
        status: HandlerStatus,
        started_at: datetime,
        duration_ms: int,
        kind: str = "stage",
        stage: str | None = None,
        provider: str | None = None,
        attempts: int = 1,
        cache_hit: bool = False,
        artifacts: Sequence[str] = (),
        summary: str | None = None,
        details: Mapping[str, Any] | None = None,
        warnings: Sequence[str] = (),
        error: str | None = None,
    ) -> HandlerReport:
        target_stage = (stage or self._current_stage).strip().casefold()
        handler_status = status
        duration = 0 if handler_status is HandlerStatus.CACHED else max(0, int(duration_ms))
        report = HandlerReport(
            scope_id=self.context.scope_id,
            collection_id=self.context.collection_id,
            item_id=self.context.item_id,
            position=self.context.position,
            capability_id=capability_id,
            provider=provider,
            kind=kind,
            stage=target_stage,
            stage_index=STAGE_INDEX[target_stage],
            status=handler_status,
            started_at=started_at,
            duration_ms=duration,
            attempts=attempts,
            cache_hit=cache_hit or handler_status is HandlerStatus.CACHED,
            artifacts=tuple(artifacts),
            summary=summary or capability_id,
            details=dict(details or {}),
            warnings=tuple(warnings),
            error=error,
        )
        self.handler(report)
        return report


def handler_status_for(status: str) -> HandlerStatus:
    """Map executor/persisted step status strings to report statuses."""

    normalized = str(status).strip().casefold()
    try:
        return HandlerStatus(normalized)
    except ValueError:
        return HandlerStatus.FAILED


__all__ = ["ProgressBoard", "ProgressTracker", "handler_status_for", "utc_now"]
