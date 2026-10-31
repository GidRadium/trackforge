"""Structured event and executor handler-report contract tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trackforge.application.dto.progress import (
    HandlerReport,
    HandlerStatus,
    ItemProgress,
    ProgressContext,
)
from trackforge.application.ports.execution import ArtifactDraft, StepResult
from trackforge.application.services.progress import ProgressBoard
from trackforge.domain.model.capabilities import ArtifactType
from trackforge.domain.model.ids import ItemId, RunId, new_uuid7
from trackforge.pipeline.events import Event, EventType

pytestmark = pytest.mark.unit

MEDIA = ArtifactType.parse("media.raw.v1")
PROBE = ArtifactType.parse("audio.probe.v1")


def test_step_progress_event_carries_structured_fields() -> None:
    event = Event(
        type=EventType.STEP_PROGRESS,
        run_id=RunId(new_uuid7()),
        item_id=ItemId(new_uuid7()),
        capability="whisper",
        status="running",
        progress=0.42,
        item_position=2,
        item_total=5,
        item_label="Collide",
        stage="observe",
        stage_index=3,
        stage_total=7,
        message="10s / 24s",
    )
    payload = event.to_dict()

    assert payload["progress"] == pytest.approx(0.42)
    assert payload["item_position"] == 2
    assert payload["item_total"] == 5
    assert payload["item_label"] == "Collide"
    assert payload["stage"] == "observe"
    assert payload["stage_index"] == 3
    assert payload["stage_total"] == 7
    assert payload["message"] == "10s / 24s"


def test_event_stage_index_is_inferred_and_unknown_stage_rejected() -> None:
    event = Event(
        type=EventType.STEP_STARTED,
        run_id=RunId(new_uuid7()),
        stage="acquire",
    )
    assert event.stage_index == 2
    with pytest.raises(ValueError):
        Event(
            type=EventType.STEP_STARTED,
            run_id=RunId(new_uuid7()),
            stage="not-a-stage",
        )


@pytest.mark.asyncio
async def test_executor_emits_reports_and_progress_ticks(
    make_manifest,
    make_capability,
    make_plan,
    make_executor,
    fake_runs,
) -> None:
    board = ProgressBoard()
    context = ProgressContext(
        scope_id="collection-1",
        collection_id="collection-1",
        item_id=str(ItemId(new_uuid7())),
        position=1,
        total=2,
        label="Collide",
        stage="observe",
        stage_index=3,
    )

    def behavior(ctx):
        ctx.progress(0.5, "half way")
        return StepResult(
            artifacts=(ArtifactDraft(type=PROBE, payload={"ok": True}),),
            summary="aac, 231.9 s, 128 kbps",
            details={"codec": "aac", "lyrics": "must never leak"},
        )

    capability = make_capability(
        make_manifest("probe", provides=(PROBE,)),
        behaviors=[behavior],
    )
    plan = make_plan([capability], [PROBE], required=[PROBE])
    executor = make_executor([capability], runs=fake_runs)
    item_id = ItemId(context.item_id or new_uuid7())

    result = await executor.run(
        plan,
        item_id=item_id,
        progress=board,
        progress_context=context,
    )

    assert len(result.handler_reports) == 1
    report = result.handler_reports[0]
    assert report.capability_id == "probe"
    assert report.status is HandlerStatus.SUCCEEDED
    assert report.summary == "aac, 231.9 s, 128 kbps"
    assert report.details["codec"] == "aac"
    assert "lyrics" not in report.details
    assert report.position == 1
    assert report.stage == "observe"
    assert board.reports("collection-1") == (report,)

    progress_events = [
        event
        for event in executor.events.history(result.run_id)
        if event.type is EventType.STEP_PROGRESS
    ]
    assert progress_events
    assert progress_events[0].progress == pytest.approx(0.5)
    assert progress_events[0].item_label == "Collide"
    assert progress_events[0].message == "half way"

    snapshot = board.snapshot("collection-1")
    assert snapshot.items
    # The terminal step event clears the transient percentage but keeps the
    # last capability message visible.
    assert snapshot.items[0].percent is None
    assert "probe" in (snapshot.items[0].message or "")


@pytest.mark.asyncio
async def test_cached_executor_report_has_zero_duration(
    make_manifest,
    make_capability,
    make_plan,
    make_executor,
    fake_runs,
) -> None:
    capability = make_capability(make_manifest("probe", provides=(PROBE,)))
    plan = make_plan([capability], [PROBE], required=[PROBE])
    executor = make_executor([capability], runs=fake_runs)
    item_id = ItemId(new_uuid7())

    first = await executor.run(plan, item_id=item_id)
    second = await executor.run(plan, item_id=item_id)

    assert first.handler_reports[0].status is HandlerStatus.SUCCEEDED
    cached = second.handler_reports[0]
    assert cached.status is HandlerStatus.CACHED
    assert cached.duration_ms == 0
    assert cached.cache_hit is True


def test_handler_report_truncates_and_sanitizes() -> None:
    report = HandlerReport(
        scope_id="collection-1",
        collection_id="collection-1",
        item_id="item-1",
        position=1,
        capability_id="probe",
        provider="ffprobe",
        kind="observer",
        stage="observe",
        stage_index=3,
        status=HandlerStatus.SUCCEEDED,
        started_at=datetime(2026, 10, 22, tzinfo=UTC),
        duration_ms=10,
        attempts=1,
        cache_hit=False,
        artifacts=("audio.probe.v1",),
        summary="x" * 500,
        details={
            "codec": "aac",
            "lyrics": "secret lyrics",
            "description": "secret description",
            "nested": {"token": "abc", "safe": "ok"},
            "url": "https://example.com/x?token=secret&v=1",
        },
    )
    assert len(report.summary) <= 200
    assert "lyrics" not in report.details
    assert "description" not in report.details
    assert "token" not in report.details["nested"]
    assert "secret" not in str(report.details["url"])
    assert report.to_dict()["summary"] == report.summary


def test_item_progress_without_measurement_shows_ellipsis() -> None:
    item = ItemProgress(
        scope_id="s",
        collection_id="s",
        item_id=None,
        position=1,
        total=1,
        label="x",
        stage="observe",
        stage_index=3,
    )
    assert item.percent_text() == "…"
