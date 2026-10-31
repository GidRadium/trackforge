"""Handler report DTO and StepResult summary/details contract tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trackforge.application.dto.collections import CollectionRunResult, CollectionRunStatus
from trackforge.application.dto.db import RunStatus
from trackforge.application.dto.progress import HandlerReport, HandlerStatus
from trackforge.application.dto.results import RunResult
from trackforge.application.ports.execution import (
    ArtifactDraft,
    StepResult,
    StepResultStatus,
)
from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model.capabilities import ArtifactType
from trackforge.interfaces.cli.presenters import to_jsonable

pytestmark = pytest.mark.unit

PROBE = ArtifactType.parse("audio.probe.v1")


def _report(**overrides) -> HandlerReport:
    values = {
        "scope_id": "collection-1",
        "collection_id": "collection-1",
        "item_id": "item-1",
        "position": 1,
        "capability_id": "probe",
        "provider": "ffprobe",
        "kind": "observer",
        "stage": "observe",
        "stage_index": 3,
        "status": HandlerStatus.SUCCEEDED,
        "started_at": datetime(2026, 10, 22, tzinfo=UTC),
        "duration_ms": 100,
        "attempts": 1,
        "cache_hit": False,
        "artifacts": ("audio.probe.v1",),
        "summary": "aac, 231.9 s, 128 kbps",
    }
    values.update(overrides)
    return HandlerReport(**values)  # type: ignore[arg-type]


def test_step_result_carries_optional_summary_and_details() -> None:
    result = StepResult(
        artifacts=(ArtifactDraft(type=PROBE, payload={"ok": True}),),
        summary="aac, 231.9 s",
        details={"codec": "aac"},
    )
    assert result.summary == "aac, 231.9 s"
    assert result.details == {"codec": "aac"}


def test_step_result_rejects_non_json_details() -> None:
    with pytest.raises(DomainValidationError):
        StepResult(details={"bad": object()})  # type: ignore[dict-item]


def test_skipped_result_can_carry_reason() -> None:
    result = StepResult.skipped(summary="invalid API key", details={"reason": "missing_key"})
    assert result.status is StepResultStatus.SKIPPED
    assert result.summary == "invalid API key"
    assert result.details == {"reason": "missing_key"}


def test_report_truncates_summary_grapheme_safe() -> None:
    report = _report(summary="é" * 500)
    assert len(report.summary) <= 200
    assert report.summary.endswith("…")


def test_report_removes_private_detail_payloads() -> None:
    report = _report(
        details={
            "codec": "aac",
            "lyrics": "line",
            "description": "private",
            "cookies": "secret",
            "nested": {"token": "x", "safe": 1},
            "url": "https://example.com/a?token=abc&v=1",
        }
    )
    assert report.details == {
        "codec": "aac",
        "nested": {"safe": 1},
        "url": "https://example.com/a",
    }


def test_report_cached_duration_and_status() -> None:
    report = _report(status=HandlerStatus.CACHED, duration_ms=250, cache_hit=False)
    assert report.duration_ms == 0
    assert report.cache_hit is True


def test_report_to_dict_includes_all_fields() -> None:
    payload = _report().to_dict()
    assert payload["capability_id"] == "probe"
    assert payload["status"] == "succeeded"
    assert payload["stage_index"] == 3
    assert payload["artifacts"] == ["audio.probe.v1"]
    assert payload["details"] == {}


def test_report_requires_matching_stage_index() -> None:
    with pytest.raises(ValueError):
        _report(stage="observe", stage_index=2)


def test_run_results_serialize_handler_reports() -> None:
    report = _report()
    run = RunResult(
        run_id="run-1",
        status=RunStatus.COMPLETED,
        handler_reports=(report,),
    )
    collection = CollectionRunResult(
        collection_id="collection-1",
        status=CollectionRunStatus.COMPLETED,
        handler_reports=(report,),
    )

    run_payload = to_jsonable(run)
    collection_payload = to_jsonable(collection)
    assert run_payload["handler_reports"][0]["capability_id"] == "probe"
    assert run_payload["handler_reports"][0]["summary"] == report.summary
    assert collection_payload["handler_reports"][0]["status"] == "succeeded"
