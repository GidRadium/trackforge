"""Feedback export tests: JSONL, CSV, filtering, redaction, and reconciliation."""

from __future__ import annotations

import csv
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from tests.application.conftest import ClaimSpec

from trackforge.application.dto.feedback import FeedbackExportResult
from trackforge.application.use_cases.export_feedback import CSV_COLUMNS
from trackforge.bootstrap.container import CatalogContext
from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model.feedback import (
    CorrectionScope,
    FeedbackEvent,
    FeedbackKind,
)
from trackforge.domain.model.ids import FeedbackEventId, ItemId, new_uuid7

pytestmark = pytest.mark.asyncio


async def _reviewed_item(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    append_claim: Callable[..., Any],
) -> ItemId:
    item_id = await create_item()
    await append_claim(item_id, ClaimSpec(field="title", value="Original"))
    await append_claim(item_id, ClaimSpec(field="artist", value="Artist"))
    await catalog_context.resolve_item().resolve(item_id)
    await catalog_context.review_item().review(
        item_id,
        action="edit",
        field="title",
        value="Corrected",
        lock=True,
        reason_code="wrong_title",
        reason_text="noise suffix",
    )
    return item_id


async def test_export_jsonl_is_parseable_and_contains_correction(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    append_claim: Callable[..., Any],
    tmp_path: Path,
) -> None:
    item_id = await _reviewed_item(catalog_context, create_item, append_claim)
    output = tmp_path / "exports" / "feedback.jsonl"

    result = await catalog_context.export_feedback().export(
        format="jsonl",
        output=output,
    )

    assert isinstance(result, FeedbackExportResult)
    assert result.output_path == output
    assert result.event_count == 1
    assert result.correction_count == 1
    assert result.jsonl_events == 1
    assert result.db_events == 1
    lines = [json.loads(line) for line in output.read_text().splitlines() if line]
    assert len(lines) == 1
    event = lines[0]
    assert event["kind"] == "correction"
    assert event["item_id"] == str(item_id)
    assert event["field"] == "title"
    assert event["user_value"] == "Corrected"
    assert event["reason_code"] == "wrong_title"
    assert event["context_snapshot"]["system_value"] == "Original"
    serialized = json.dumps(event).lower()
    assert "audio" not in serialized
    assert "cookie" not in serialized


async def test_export_csv_has_stable_columns_and_filters(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    append_claim: Callable[..., Any],
    tmp_path: Path,
) -> None:
    item_id = await _reviewed_item(catalog_context, create_item, append_claim)
    output = tmp_path / "feedback.csv"

    result = await catalog_context.export_feedback().export(
        format="csv",
        output=output,
        item_id=item_id,
        field="title",
    )
    assert result.event_count == 1
    rows = list(csv.DictReader(output.read_text().splitlines()))
    assert len(rows) == 1
    assert tuple(rows[0]) == CSV_COLUMNS
    assert rows[0]["item_id"] == str(item_id)
    assert rows[0]["user_value"] == "Corrected"

    empty = await catalog_context.export_feedback().export(
        format="csv",
        output=tmp_path / "empty.csv",
        item_id=new_uuid7(),
    )
    assert empty.event_count == 0
    header = (tmp_path / "empty.csv").read_text().strip()
    assert header == ",".join(CSV_COLUMNS)


async def test_export_merges_db_only_events_with_a_warning(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    tmp_path: Path,
) -> None:
    item_id = await create_item()
    event = FeedbackEvent(
        id=FeedbackEventId(new_uuid7()),
        kind=FeedbackKind.APPROVAL,
        item_id=item_id,
        field="title",
        system_value="Title",
        user_value="Title",
        scope=CorrectionScope.ITEM,
        created_at=datetime.now(UTC),
    )
    async with catalog_context.unit_of_work() as uow:
        await uow.feedback.append_event(event)

    result = await catalog_context.export_feedback().export(
        format="jsonl",
        output=tmp_path / "db-only.jsonl",
    )

    assert result.event_count == 1
    assert result.jsonl_events == 0
    assert result.db_events == 1
    assert any("only in SQLite" in warning for warning in result.warnings)
    line = json.loads((tmp_path / "db-only.jsonl").read_text().strip())
    assert line["id"] == str(event.id)


async def test_export_since_filters_and_invalid_since_is_user_error(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    append_claim: Callable[..., Any],
    tmp_path: Path,
) -> None:
    await _reviewed_item(catalog_context, create_item, append_claim)
    future = datetime.now(UTC) + timedelta(days=1)
    result = await catalog_context.export_feedback().export(
        format="jsonl",
        output=tmp_path / "future.jsonl",
        since=future,
    )
    assert result.event_count == 0

    with pytest.raises(DomainValidationError):
        await catalog_context.export_feedback().export(
            format="jsonl",
            output=tmp_path / "bad.jsonl",
            since="not-a-date",
        )


async def test_export_redacts_secrets_from_context(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    tmp_path: Path,
) -> None:
    item_id = await create_item()
    event = FeedbackEvent(
        id=FeedbackEventId(new_uuid7()),
        kind=FeedbackKind.APPROVAL,
        item_id=item_id,
        field="title",
        system_value="Title",
        user_value="Title",
        context_snapshot={
            "api_key": "sk-very-secret-value",
            "note": "token=abcdef123456",
            "claims": [],
        },
        created_at=datetime.now(UTC),
    )
    async with catalog_context.unit_of_work() as uow:
        await uow.feedback.append_event(event)

    output = tmp_path / "redacted.jsonl"
    await catalog_context.export_feedback().export(format="jsonl", output=output)
    text = output.read_text()
    assert "sk-very-secret-value" not in text
    assert "abcdef123456" not in text
    payload = json.loads(text.strip())
    assert payload["context_snapshot"]["api_key"] == "***"


async def test_export_refuses_to_overwrite_the_journal(
    catalog_context: CatalogContext,
    tmp_path: Path,
) -> None:
    events_path = Path(catalog_context.config.data_dir) / "feedback" / "events.jsonl"

    with pytest.raises(DomainValidationError):
        await catalog_context.export_feedback().export(
            format="jsonl",
            output=events_path,
        )
    assert not events_path.exists()
