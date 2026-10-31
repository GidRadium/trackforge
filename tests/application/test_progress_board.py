"""Unit tests for the in-memory progress board and tracker."""

from __future__ import annotations

from dataclasses import replace

import pytest

from trackforge.application.dto.progress import (
    CollectionProgressStatus,
    HandlerReport,
    HandlerStatus,
    ItemProgress,
    ProgressContext,
    ProgressStatus,
)
from trackforge.application.services.progress import ProgressBoard, ProgressTracker
from trackforge.domain.model._validation import utc_now

pytestmark = pytest.mark.unit


def _context(*, position: int = 1, total: int = 2, label: str = "Collide") -> ProgressContext:
    return ProgressContext(
        scope_id="collection-1",
        collection_id="collection-1",
        item_id=None,
        position=position,
        total=total,
        label=label,
    )


def _item(
    context: ProgressContext,
    *,
    status: ProgressStatus = ProgressStatus.RUNNING,
    stage: str = "observe",
    percent: float | None = None,
    message: str | None = None,
) -> ItemProgress:
    from trackforge.application.dto.progress import STAGE_INDEX

    return ItemProgress(
        scope_id=context.scope_id,
        collection_id=context.collection_id,
        item_id=context.item_id,
        position=context.position,
        total=context.total,
        label=context.label,
        stage=stage,
        stage_index=STAGE_INDEX[stage],
        status=status,
        percent=percent,
        message=message,
    )


def _report(
    context: ProgressContext,
    *,
    capability: str = "probe",
    stage: str = "observe",
    status: HandlerStatus = HandlerStatus.SUCCEEDED,
    duration_ms: int = 120,
) -> HandlerReport:
    from trackforge.application.dto.progress import STAGE_INDEX

    return HandlerReport(
        scope_id=context.scope_id,
        collection_id=context.collection_id,
        item_id=context.item_id,
        position=context.position,
        capability_id=capability,
        provider="ffprobe",
        kind="observer",
        stage=stage,
        stage_index=STAGE_INDEX[stage],
        status=status,
        started_at=utc_now(),
        duration_ms=duration_ms,
        attempts=1,
        cache_hit=False,
        artifacts=("audio.probe.v1",),
        summary="aac, 231.9 s, 128 kbps",
    )


def test_board_snapshot_counts_and_sorting() -> None:
    board = ProgressBoard()
    first = _context(position=2, total=2, label="Second")
    second = _context(position=1, total=2, label="First")
    board.start_collection("collection-1", (_item(first, status=ProgressStatus.QUEUED),))
    board.update(_item(second, status=ProgressStatus.QUEUED))
    board.update(_item(second, status=ProgressStatus.RUNNING, percent=0.4))
    board.update(_item(second, status=ProgressStatus.DONE))

    snapshot = board.snapshot("collection-1")

    assert snapshot.collection_id == "collection-1"
    assert [item.label for item in snapshot.items] == ["First", "Second"]
    assert snapshot.completed == 1
    assert snapshot.remaining == 1
    assert snapshot.status is CollectionProgressStatus.RUNNING


def test_board_keeps_only_latest_item_state() -> None:
    board = ProgressBoard()
    context = _context()
    board.start_collection("collection-1", (_item(context, status=ProgressStatus.QUEUED),))
    for index in range(50):
        board.update(_item(context, percent=index / 50))
    snapshot = board.snapshot("collection-1")
    assert len(snapshot.items) == 1
    assert snapshot.items[0].percent == pytest.approx(0.98)


def test_board_completes_and_marks_partial_or_failed() -> None:
    board = ProgressBoard()
    first = _context(position=1)
    second = _context(position=2, label="Chaos")
    board.start_collection("collection-1", (_item(first, status=ProgressStatus.QUEUED),))
    board.update(_item(first, status=ProgressStatus.DONE))
    board.update(_item(second, status=ProgressStatus.FAILED, message="download failed"))
    snapshot = board.snapshot("collection-1")
    assert snapshot.status is CollectionProgressStatus.PARTIAL
    assert snapshot.failed == 1
    assert snapshot.remaining == 0
    assert snapshot.items[1].message == "download failed"


def test_board_reports_and_aggregate_capability_state() -> None:
    board = ProgressBoard()
    context = _context()
    board.start_collection("collection-1", (_item(context, status=ProgressStatus.QUEUED),))
    board.handler(_report(context))
    board.handler(_report(context, capability="musicbrainz_search", stage="observe"))
    reports = board.reports("collection-1")

    assert [report.capability_id for report in reports] == ["probe", "musicbrainz_search"]
    assert all(report.position == 1 for report in reports)


def test_board_handler_replaces_same_capability_for_resume() -> None:
    board = ProgressBoard()
    context = _context()
    board.start_collection("collection-1", (_item(context, status=ProgressStatus.QUEUED),))
    board.handler(_report(context, duration_ms=10))
    board.handler(_report(context, duration_ms=99))
    reports = board.reports("collection-1")
    assert len(reports) == 1
    assert reports[0].duration_ms == 99


def test_board_unknown_snapshot_is_empty_and_never_raises() -> None:
    board = ProgressBoard()
    snapshot = board.snapshot("missing")
    assert snapshot.items == ()
    assert snapshot.status is CollectionProgressStatus.QUEUED
    assert board.reports("missing") == ()


def test_item_label_is_truncated_and_sanitized() -> None:
    context = _context(label="x" * 500 + "\x00\n" + "é" * 10)
    item = _item(context)
    assert len(item.label) <= 80
    assert "\x00" not in item.label
    assert "\n" not in item.label


def test_percent_is_measured_or_none() -> None:
    context = _context()
    assert _item(context, percent=None).percent is None
    with pytest.raises(ValueError):
        _item(context, percent=1.5)
    with pytest.raises(ValueError):
        _item(context, percent=-0.1)


class _BrokenSink:
    def update(self, item: ItemProgress) -> None:
        raise RuntimeError("renderer is closed")

    def handler(self, report: HandlerReport) -> None:
        raise RuntimeError("renderer is closed")


def test_tracker_survives_broken_sink_and_updates_context_stage() -> None:
    tracker = ProgressTracker(_BrokenSink(), _context())
    tracker.stage("observe", percent=0.5, message="musicbrainz")
    tracker.add_warning("no_data")
    report = tracker.report(
        capability_id="probe",
        status=HandlerStatus.SUCCEEDED,
        started_at=utc_now(),
        duration_ms=150,
        stage="observe",
        summary="aac",
    )
    tracker.failed("boom")

    assert tracker.current_stage == "observe"
    assert tracker.context.stage == "observe"
    assert tracker.context.stage_index == 3
    assert report.duration_ms == 150


def test_tracker_cached_report_has_zero_duration() -> None:
    board = ProgressBoard()
    context = _context()
    tracker = ProgressTracker(board, context)
    report = tracker.report(
        capability_id="probe",
        status=HandlerStatus.CACHED,
        started_at=utc_now(),
        duration_ms=999,
        stage="observe",
        summary="cached",
    )
    assert report.duration_ms == 0
    assert report.cache_hit is True


def test_snapshot_marks_forced_cancelled_status() -> None:
    board = ProgressBoard()
    context = _context()
    board.start_collection("collection-1", (_item(context, status=ProgressStatus.QUEUED),))
    board.mark_collection("collection-1", CollectionProgressStatus.CANCELLED)
    assert board.snapshot("collection-1").status is CollectionProgressStatus.CANCELLED


class _Exploding:
    @property
    def collection_id(self) -> str:
        raise RuntimeError("boom")


def test_board_never_raises_for_broken_inputs() -> None:
    board = ProgressBoard()
    board.update(_Exploding())  # type: ignore[arg-type]
    board.handler(_Exploding())  # type: ignore[arg-type]
    assert board.snapshot("none").items == ()


def test_item_is_immutable_and_replace_works() -> None:
    context = _context()
    item = _item(context, percent=0.25)
    changed = replace(item, percent=0.75)
    assert item.percent == 0.25
    assert changed.percent == 0.75
