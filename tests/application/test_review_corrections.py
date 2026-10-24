"""Review corrections, locks, sessions, snapshots, and JSONL journal tests."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from tests.application.conftest import ClaimSpec

from trackforge.adapters.persistence.feedback.jsonl import (
    JsonlFeedbackJournal,
    correction_document,
    event_document,
)
from trackforge.application.dto.views import ReviewView
from trackforge.application.ports.feedback import FeedbackConsistency
from trackforge.application.services.context_snapshot import (
    build_context_snapshot,
    redact_secrets,
)
from trackforge.application.use_cases.review_item import ReviewItem
from trackforge.bootstrap.container import CatalogContext
from trackforge.domain.errors import (
    DomainValidationError,
    DuplicateCorrection,
    FeedbackStorageError,
    UnsupportedScope,
)
from trackforge.domain.model.feedback import (
    Correction,
    CorrectionScope,
    FeedbackEvent,
    FeedbackKind,
    ReasonCode,
)
from trackforge.domain.model.ids import (
    CorrectionId,
    DecisionId,
    FeedbackEventId,
    ItemId,
    new_uuid7,
)


async def _prepared_item(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    append_claim: Callable[..., Any],
    *,
    title: str = "Original Song",
) -> ItemId:
    item_id = await create_item()
    await append_claim(item_id, ClaimSpec(field="title", value=title))
    await append_claim(
        item_id,
        ClaimSpec(field="title", value=f"{title}!", source="path", confidence=0.2),
    )
    await append_claim(item_id, ClaimSpec(field="artist", value="Original Artist"))
    await append_claim(item_id, ClaimSpec(field="album", value="Original Album"))
    await append_claim(item_id, ClaimSpec(field="albumartist", value="Original Artist"))
    await catalog_context.resolve_item().resolve(item_id)
    return item_id


@pytest.mark.asyncio
async def test_edit_with_lock_creates_claim_correction_event_session_and_jsonl(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    append_claim: Callable[..., Any],
) -> None:
    item_id = await _prepared_item(catalog_context, create_item, append_claim)

    view = await catalog_context.review_item().review(
        item_id,
        action="edit",
        field="title",
        value="Reviewed Title",
        lock=True,
        reason_code="wrong_title",
        reason_text="title had a noise suffix",
    )

    assert isinstance(view, ReviewView)
    assert view.action == "edit"
    assert view.correction is not None
    assert view.correction.user_value == "Reviewed Title"
    assert view.correction.system_value == "Original Song"
    assert view.correction.reason_code is ReasonCode.WRONG_TITLE
    assert view.correction.review_session_id is not None
    assert view.session is not None
    assert view.requires_render is True
    assert view.field("title") is not None
    assert view.field("title").current == "Reviewed Title"  # type: ignore[union-attr]
    assert view.field("title").locked is True  # type: ignore[union-attr]

    async with catalog_context.unit_of_work() as uow:
        item = await uow.items.get(item_id)
        claims = await uow.claims.list_by_item(item_id)
        locks = await uow.locks.list_by_item(item_id)
        records = await uow.decisions.list_records(item_id)
        events = await uow.feedback.list_by_item(item_id)
        sessions = await uow.feedback.list_sessions(item_id=item_id)

    assert item is not None
    user_claims = [claim for claim in claims if claim.source == "user"]
    assert len(user_claims) == 1
    assert user_claims[0].value == "Reviewed Title"
    assert user_claims[0].method == "manual_override"
    assert self_confidence(user_claims[0]) == 1.0
    assert any(lock.field == "title" and lock.value == "Reviewed Title" for lock in locks)
    assert [record.version for record in records] == [1, 2]
    assert records[-1].decision_for("title").locked is True  # type: ignore[union-attr]
    assert len(events) == 1
    assert events[0].kind is FeedbackKind.CORRECTION
    assert events[0].reason_code is ReasonCode.WRONG_TITLE
    assert events[0].review_session_id == view.session.id
    assert events[0].context_snapshot is not None
    assert events[0].context_snapshot["system_value"] == "Original Song"
    assert events[0].context_snapshot["user_value"] == "Reviewed Title"
    assert events[0].context_snapshot["system_source"] == "initial_tags"
    assert len(sessions) == 1
    assert sessions[0].finished_at is not None
    assert sessions[0].corrections == (view.correction.id,)

    events_path = Path(catalog_context.config.data_dir) / "feedback" / "events.jsonl"
    corrections_dir = Path(catalog_context.config.data_dir) / "feedback" / "corrections"
    lines = [json.loads(line) for line in events_path.read_text().splitlines() if line]
    assert len(lines) == 1
    assert lines[0]["id"] == str(view.correction.id)
    assert lines[0]["kind"] == "correction"
    assert lines[0]["user_value"] == "Reviewed Title"
    assert lines[0]["reason_code"] == "wrong_title"
    correction_path = corrections_dir / f"{view.correction.id}.json"
    assert correction_path.is_file()
    correction_payload = json.loads(correction_path.read_text())
    assert correction_payload["context_snapshot"]["alternatives"]
    assert "audio" not in json.dumps(correction_payload).lower()


def self_confidence(claim: Any) -> float:
    return float(claim.confidence)


@pytest.mark.asyncio
async def test_accept_records_approval_events_without_changing_decisions(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    append_claim: Callable[..., Any],
) -> None:
    item_id = await _prepared_item(catalog_context, create_item, append_claim)
    async with catalog_context.unit_of_work() as uow:
        before = await uow.decisions.list_records(item_id)

    view = await catalog_context.review_item().review(item_id, action="accept")

    assert view.action == "accept"
    assert view.correction is None
    assert view.requires_render is False
    async with catalog_context.unit_of_work() as uow:
        after = await uow.decisions.list_records(item_id)
        events = await uow.feedback.list_by_item(item_id)
    assert len(after) == len(before)
    assert events
    assert all(event.kind is FeedbackKind.APPROVAL for event in events)
    assert all(event.context_snapshot for event in events)


@pytest.mark.asyncio
async def test_unlock_removes_lock_but_never_claims_or_corrections(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    append_claim: Callable[..., Any],
) -> None:
    item_id = await _prepared_item(catalog_context, create_item, append_claim)
    reviewed = await catalog_context.review_item().review(
        item_id,
        action="edit",
        field="title",
        value="Locked Title",
        lock=True,
    )
    assert reviewed.correction is not None
    async with catalog_context.unit_of_work() as uow:
        claims_before = await uow.claims.list_by_item(item_id)
        events_before = await uow.feedback.list_by_item(item_id)

    unlocked = await catalog_context.review_item().review(
        item_id,
        action="unlock",
        field="title",
    )

    assert unlocked.lock is None
    assert "title" not in unlocked.locked_fields
    async with catalog_context.unit_of_work() as uow:
        claims_after = await uow.claims.list_by_item(item_id)
        events_after = await uow.feedback.list_by_item(item_id)
        locks = await uow.locks.list_by_item(item_id)
    assert {claim.id for claim in claims_before}.issubset({claim.id for claim in claims_after})
    assert {event.id for event in events_before}.issubset({event.id for event in events_after})
    assert not [lock for lock in locks if lock.field == "title"]
    assert any(event.kind is FeedbackKind.UNLOCK for event in events_after)
    correction_path = (
        Path(catalog_context.config.data_dir)
        / "feedback"
        / "corrections"
        / f"{reviewed.correction.id}.json"
    )
    assert correction_path.is_file()


@pytest.mark.asyncio
async def test_unknown_sets_null_decision_and_records_reason(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    append_claim: Callable[..., Any],
) -> None:
    item_id = await _prepared_item(catalog_context, create_item, append_claim)

    view = await catalog_context.review_item().review(
        item_id,
        action="unknown",
        field="album",
        reason_code="wrong_album",
        reason_text="album is not known",
    )

    assert view.field("album").current is None  # type: ignore[union-attr]
    assert view.correction is not None
    assert view.correction.user_value is None
    assert view.correction.reason_code is ReasonCode.WRONG_ALBUM
    async with catalog_context.unit_of_work() as uow:
        record = await uow.decisions.get_record(
            (await uow.items.get(item_id)).decision_record_id  # type: ignore[union-attr]
        )
    assert record is not None
    assert record.decision_for("album").value is None  # type: ignore[union-attr]
    assert record.decision_for("album").locked is True  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_manual_mode_uses_user_claim_and_marks_decided_by_user(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    append_claim: Callable[..., Any],
) -> None:
    item_id = await _prepared_item(catalog_context, create_item, append_claim)
    await catalog_context.review_item().review(
        item_id,
        action="edit",
        field="title",
        value="Manual Title",
    )

    resolved = await catalog_context.resolve_item().resolve(item_id, mode="manual")

    assert resolved.fields["title"] == "Manual Title"
    async with catalog_context.unit_of_work() as uow:
        record = await uow.decisions.get_record(resolved.decision_record_id)
    assert record is not None
    title = record.decision_for("title")
    assert title is not None
    assert title.mode.value == "manual"
    assert title.decided_by.value == "user"
    assert title.locked is False


@pytest.mark.asyncio
async def test_journal_failure_does_not_mutate_claims_decisions_or_locks(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    append_claim: Callable[..., Any],
) -> None:
    item_id = await _prepared_item(catalog_context, create_item, append_claim)

    class FailingJournal:
        async def append_event(self, *args: Any, **kwargs: Any) -> None:
            raise FeedbackStorageError("disk full")

        async def consistency(self, events: list[FeedbackEvent]) -> FeedbackConsistency:
            return FeedbackConsistency()

    service = ReviewItem(
        catalog_context.unit_of_work,
        catalog_context.resolve_item(),
        catalog_context.explain_item(),
        journal=FailingJournal(),  # type: ignore[arg-type]
    )
    async with catalog_context.unit_of_work() as uow:
        claims_before = await uow.claims.list_by_item(item_id)
        records_before = await uow.decisions.list_records(item_id)

    with pytest.raises(FeedbackStorageError):
        await service.review(
            item_id,
            action="edit",
            field="title",
            value="Should Not Persist",
            lock=True,
        )

    async with catalog_context.unit_of_work() as uow:
        claims_after = await uow.claims.list_by_item(item_id)
        records_after = await uow.decisions.list_records(item_id)
        locks = await uow.locks.list_by_item(item_id)
        events = await uow.feedback.list_by_item(item_id)
    assert [claim.id for claim in claims_after] == [claim.id for claim in claims_before]
    assert [record.id for record in records_after] == [record.id for record in records_before]
    assert not locks
    assert not events


@pytest.mark.asyncio
async def test_scope_reason_and_value_validation_are_clear(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    append_claim: Callable[..., Any],
) -> None:
    item_id = await _prepared_item(catalog_context, create_item, append_claim)
    service = catalog_context.review_item()

    with pytest.raises(UnsupportedScope):
        await service.review(item_id, action="edit", field="title", value="x", scope="global")
    with pytest.raises(DomainValidationError):
        await service.review(item_id, action="edit", field="title", value="   ")
    with pytest.raises(DomainValidationError):
        await service.review(
            item_id,
            action="edit",
            field="title",
            value="x",
            reason_code="not_a_reason",
        )
    with pytest.raises(DomainValidationError):
        await service.review(item_id, action="edit", field="not_a_field", value="x")

    async with catalog_context.unit_of_work() as uow:
        assert await uow.feedback.list_by_item(item_id) == []


@pytest.mark.asyncio
async def test_edit_rejects_cross_field_invalid_value_without_mutation(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    append_claim: Callable[..., Any],
) -> None:
    item_id = await create_item()
    await append_claim(item_id, ClaimSpec(field="title", value="Song"))
    await append_claim(item_id, ClaimSpec(field="track", value=1))
    await append_claim(item_id, ClaimSpec(field="tracktotal", value=2))
    await catalog_context.resolve_item().resolve(item_id)
    async with catalog_context.unit_of_work() as uow:
        records_before = await uow.decisions.list_records(item_id)

    with pytest.raises(DomainValidationError):
        await catalog_context.review_item().review(
            item_id,
            action="edit",
            field="track",
            value="5",
            lock=True,
        )

    async with catalog_context.unit_of_work() as uow:
        records_after = await uow.decisions.list_records(item_id)
        assert await uow.feedback.list_by_item(item_id) == []
        assert not await uow.locks.list_by_item(item_id)
    assert [record.id for record in records_after] == [record.id for record in records_before]


@pytest.mark.asyncio
async def test_edit_coerces_numeric_field_when_current_decision_is_unknown(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    append_claim: Callable[..., Any],
) -> None:
    item_id = await create_item()
    await append_claim(item_id, ClaimSpec(field="title", value="Song"))
    await catalog_context.resolve_item().resolve(item_id)

    view = await catalog_context.review_item().review(
        item_id,
        action="edit",
        field="track",
        value="7",
        lock=True,
    )

    track = view.field("track")
    assert track is not None
    assert track.current == 7
    assert isinstance(track.current, int)


def test_context_snapshot_redacts_secrets_and_audio() -> None:
    snapshot = build_context_snapshot(
        item_id=new_uuid7(),
        field="title",
        system_value="System",
        user_value="User",
        extra={
            "api_key": "sk-super-secret-value",
            "note": "token=abcdef123456",
            "audio_bytes": b"\x00\x01",
            "nested": {"authorization": "Bearer top-secret-token"},
        },
    )
    serialized = json.dumps(snapshot)
    assert snapshot["api_key"] == "***"
    assert snapshot["audio_bytes"] == "***"
    assert "sk-super-secret-value" not in serialized
    assert "abcdef123456" not in serialized
    assert "top-secret-token" not in serialized
    assert redact_secrets(snapshot) == snapshot


def test_jsonl_append_is_canonical_and_duplicate_correction_is_rejected(
    tmp_path: Path,
) -> None:
    journal = JsonlFeedbackJournal(tmp_path / "data")
    now = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
    item_id = ItemId(new_uuid7())
    decision_id = DecisionId(new_uuid7())
    correction_id = CorrectionId(new_uuid7())
    correction = Correction(
        id=correction_id,
        item_id=item_id,
        field="title",
        system_decision_id=decision_id,
        system_value="Old",
        user_value="New",
        scope=CorrectionScope.ITEM,
        reason_code=ReasonCode.WRONG_TITLE,
        context_snapshot={"claims": [], "system_source": "initial_tags"},
        created_at=now,
        review_session_id=None,
    )
    event = FeedbackEvent(
        id=FeedbackEventId(str(correction_id)),
        kind=FeedbackKind.CORRECTION,
        item_id=item_id,
        field="title",
        system_value="Old",
        user_value="New",
        scope=CorrectionScope.ITEM,
        reason_code=ReasonCode.WRONG_TITLE,
        context_snapshot=dict(correction.context_snapshot),
        created_at=now,
        system_decision_id=decision_id,
    )

    journal.append_event_sync(event, correction=correction)
    raw_line = journal.events_path.read_text().strip()
    parsed = json.loads(raw_line)
    assert raw_line == json.dumps(
        parsed,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert parsed["id"] == str(correction_id)
    assert journal.get_correction_sync(correction_id) is not None

    with pytest.raises(DuplicateCorrection):
        journal.write_correction_sync(correction)


def test_jsonl_write_failure_is_not_silent(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "feedback").write_text("not a directory")
    journal = JsonlFeedbackJournal(data_dir)
    event = FeedbackEvent(
        id=FeedbackEventId(new_uuid7()),
        kind=FeedbackKind.APPROVAL,
        item_id=ItemId(new_uuid7()),
        created_at=datetime(2026, 10, 24, tzinfo=UTC),
    )
    with pytest.raises(FeedbackStorageError):
        journal.append_event_sync(event)


def test_event_and_correction_documents_are_stable() -> None:
    event = FeedbackEvent(
        id=FeedbackEventId(new_uuid7()),
        kind=FeedbackKind.APPROVAL,
        item_id=ItemId(new_uuid7()),
        field="title",
        system_value="A",
        user_value="A",
        reason_code=ReasonCode.OTHER,
        created_at=datetime(2026, 10, 24, tzinfo=UTC),
    )
    document = event_document(event)
    assert document["schema_version"] == 1
    assert document["reason_code"] == "other"
    correction = Correction(
        id=CorrectionId(new_uuid7()),
        item_id=event.item_id,
        field="title",
        system_decision_id=DecisionId(new_uuid7()),
        system_value="A",
        user_value="B",
        reason_code=ReasonCode.OTHER,
        context_snapshot={},
        created_at=datetime(2026, 10, 24, tzinfo=UTC),
    )
    assert correction_document(correction)["scope"] == "item"
