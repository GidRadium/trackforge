"""Feedback, correction scope, and review-session tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trackforge.domain.errors import DomainValidationError, UnsupportedScope
from trackforge.domain.model.feedback import (
    Correction,
    CorrectionScope,
    FeedbackEvent,
    FeedbackKind,
    ReasonCode,
    ReviewSession,
)
from trackforge.domain.model.ids import (
    CorrectionId,
    DecisionId,
    FeedbackEventId,
    ItemId,
    ReviewSessionId,
    new_uuid7,
)

NOW = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)


def _correction(**overrides: object) -> Correction:
    data: dict[str, object] = {
        "id": CorrectionId(new_uuid7()),
        "item_id": ItemId(new_uuid7()),
        "field": "title",
        "system_decision_id": DecisionId(new_uuid7()),
        "system_value": "Song",
        "user_value": "Song (Remastered)",
        "reason_code": ReasonCode.WRONG_TITLE,
        "context_snapshot": {"claims": [], "policy_versions": []},
        "created_at": NOW,
    }
    data.update(overrides)
    return Correction(**data)  # type: ignore[arg-type]


def test_valid_item_correction() -> None:
    correction = _correction()

    assert correction.scope is CorrectionScope.ITEM
    assert correction.reason_code is ReasonCode.WRONG_TITLE
    assert correction.applied is False


@pytest.mark.parametrize(
    "scope",
    [
        CorrectionScope.SOURCE,
        CorrectionScope.ARTIST,
        CorrectionScope.PROFILE,
        CorrectionScope.GLOBAL,
    ],
)
def test_wide_correction_scope_is_rejected(scope: CorrectionScope) -> None:
    with pytest.raises(UnsupportedScope):
        _correction(scope=scope)


def test_correction_requires_context_snapshot() -> None:
    data: dict[str, object] = {
        "id": CorrectionId(new_uuid7()),
        "item_id": ItemId(new_uuid7()),
        "field": "title",
        "system_decision_id": DecisionId(new_uuid7()),
        "system_value": "Song",
        "user_value": "Song (Remastered)",
        "reason_code": ReasonCode.WRONG_TITLE,
    }

    with pytest.raises(DomainValidationError):
        Correction(**data)  # type: ignore[arg-type]


def test_correction_scope_default_and_capability() -> None:
    assert CorrectionScope.ITEM.executable_in_v1 is True
    assert CorrectionScope.GLOBAL.executable_in_v1 is False


def test_correction_rejects_non_json_context() -> None:
    with pytest.raises(DomainValidationError):
        _correction(context_snapshot={"audio": b"raw bytes"})


def test_correction_rejects_invalid_reason_code() -> None:
    with pytest.raises(DomainValidationError):
        _correction(reason_code="not_a_reason")


def test_feedback_event() -> None:
    event = FeedbackEvent(
        id=FeedbackEventId(new_uuid7()),
        kind=FeedbackKind.APPROVAL,
        item_id=ItemId(new_uuid7()),
        field="title",
        system_value="Song",
        user_value="Song",
        scope=CorrectionScope.ITEM,
        reason="user approved",
        context_snapshot={"decision": "record-1"},
        created_at=NOW,
    )

    assert event.kind is FeedbackKind.APPROVAL
    assert event.context_snapshot == {"decision": "record-1"}


def test_feedback_event_rejects_invalid_kind() -> None:
    with pytest.raises(DomainValidationError):
        FeedbackEvent(
            id=FeedbackEventId(new_uuid7()),
            kind="nope",  # type: ignore[arg-type]
            item_id=ItemId(new_uuid7()),
        )


def test_review_session_valid() -> None:
    item_id = ItemId(new_uuid7())
    session = ReviewSession(
        id=ReviewSessionId(new_uuid7()),
        item_ids=(item_id,),
        fields=("title", "artist"),
        started_at=NOW,
        finished_at=NOW + timedelta(minutes=1),
        corrections=(CorrectionId(new_uuid7()),),
        notes=("checked against source",),
    )

    assert session.item_ids == (item_id,)
    assert session.finished_at is not None


def test_review_session_finished_before_started() -> None:
    with pytest.raises(DomainValidationError):
        ReviewSession(
            id=ReviewSessionId(new_uuid7()),
            item_ids=(ItemId(new_uuid7()),),
            fields=("title",),
            started_at=NOW,
            finished_at=NOW - timedelta(seconds=1),
        )
