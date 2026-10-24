"""Feedback statistics tests: correction/review/unknown rates and grouping."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from tests.application.conftest import ClaimSpec

from trackforge.application.dto.feedback import FeedbackStatsReport
from trackforge.bootstrap.container import CatalogContext
from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model.feedback import FeedbackKind
from trackforge.domain.model.ids import ItemId

pytestmark = pytest.mark.asyncio


async def _reviewed_item(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    append_claim: Callable[..., Any],
) -> ItemId:
    item_id = await create_item()
    await append_claim(item_id, ClaimSpec(field="title", value="Original"))
    await append_claim(item_id, ClaimSpec(field="artist", value="Artist"))
    await append_claim(item_id, ClaimSpec(field="album", value="Album"))
    await append_claim(item_id, ClaimSpec(field="albumartist", value="Artist"))
    await catalog_context.resolve_item().resolve(item_id)
    await catalog_context.review_item().review(
        item_id,
        action="accept",
        field="artist",
    )
    await catalog_context.review_item().review(
        item_id,
        action="edit",
        field="title",
        value="Corrected",
        lock=True,
        reason_code="wrong_title",
    )
    return item_id


async def test_stats_counts_corrections_approvals_and_rates(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    append_claim: Callable[..., Any],
) -> None:
    item_id = await _reviewed_item(catalog_context, create_item, append_claim)

    report = await catalog_context.feedback_stats().calculate(item_id=item_id)

    assert isinstance(report, FeedbackStatsReport)
    assert report.total_events == 2
    assert report.corrections == 1
    assert report.approvals == 1
    assert report.correction_rate == pytest.approx(0.5)
    assert report.total_decisions > 0
    assert report.reviewed_items == 1
    assert report.total_items == 1
    assert report.review_rate == pytest.approx(1.0)
    assert report.accuracy is None

    title = report.by_field["title"]
    assert title.corrections == 1
    assert title.reviews == 1
    assert title.correction_rate == pytest.approx(1.0)
    assert "initial_tags" in report.by_source
    assert report.by_method


async def test_stats_group_by_source_and_method(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    append_claim: Callable[..., Any],
) -> None:
    item_id = await _reviewed_item(catalog_context, create_item, append_claim)

    by_source = await catalog_context.feedback_stats().calculate(
        group_by="source",
        item_id=item_id,
    )
    by_method = await catalog_context.feedback_stats().calculate(
        group_by="method",
        item_id=item_id,
    )

    assert by_source.group_by == "source"
    assert by_source.groups
    assert all(group.reviews >= group.corrections for group in by_source.groups.values())
    assert by_method.group_by == "method"
    assert by_method.groups
    assert set(by_method.by_field) == set(by_source.by_field)


async def test_unknown_decisions_and_review_notes_are_counted(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    append_claim: Callable[..., Any],
) -> None:
    item_id = await create_item()
    await append_claim(item_id, ClaimSpec(field="title", value="Song"))
    await append_claim(item_id, ClaimSpec(field="album", value="Album"))
    await catalog_context.resolve_item().resolve(item_id)
    await catalog_context.review_item().review(
        item_id,
        action="unknown",
        field="album",
        reason_code="wrong_album",
    )
    await catalog_context.review_item().review(
        item_id,
        action="skip",
        field="artist",
        reason="leave artist for later",
    )

    report = await catalog_context.feedback_stats().calculate(item_id=item_id)

    assert report.corrections == 1
    assert report.review_notes == 1
    assert report.unknown_decisions >= 1
    assert report.unknown_rate > 0
    assert report.by_field["album"].unknowns == 1


async def test_stats_empty_catalog_and_validation(
    catalog_context: CatalogContext,
) -> None:
    report = await catalog_context.feedback_stats().calculate()
    assert report.total_events == 0
    assert report.corrections == 0
    assert report.correction_rate == 0.0
    assert report.review_rate == 0.0
    assert report.unknown_rate == 0.0
    assert report.by_field == {}

    with pytest.raises(DomainValidationError):
        await catalog_context.feedback_stats().calculate(group_by="album")


async def test_stats_sessions_are_listed(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    append_claim: Callable[..., Any],
) -> None:
    item_id = await _reviewed_item(catalog_context, create_item, append_claim)

    sessions = await catalog_context.feedback_stats().sessions(item_id=item_id)

    assert len(sessions) == 2
    assert all(session.finished_at is not None for session in sessions)
    assert any(session.corrections for session in sessions)
    assert all(session.started_at <= datetime.now(UTC) for session in sessions)


async def test_stats_merges_db_only_event_with_warning(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
) -> None:
    from trackforge.domain.model.feedback import FeedbackEvent
    from trackforge.domain.model.ids import FeedbackEventId, new_uuid7

    item_id = await create_item()
    async with catalog_context.unit_of_work() as uow:
        await uow.feedback.append_event(
            FeedbackEvent(
                id=FeedbackEventId(new_uuid7()),
                kind=FeedbackKind.APPROVAL,
                item_id=item_id,
                field="title",
                system_value="T",
                user_value="T",
                created_at=datetime.now(UTC),
            )
        )

    report = await catalog_context.feedback_stats().calculate(item_id=item_id)
    assert report.total_events == 1
    assert any("only in SQLite" in warning for warning in report.warnings)
