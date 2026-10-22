"""Item aggregate and status-transition tests."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from trackforge.domain.errors import DomainValidationError, InvalidStateTransition
from trackforge.domain.model.item import ALLOWED_TRANSITIONS, Item, ItemStatus

NOW = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)


def test_forward_lifecycle(make_item: Callable[..., Item]) -> None:
    item = make_item()
    statuses = [
        ItemStatus.ACQUIRED,
        ItemStatus.OBSERVED,
        ItemStatus.INTERPRETED,
        ItemStatus.RESOLVED,
        ItemStatus.RENDERED,
        ItemStatus.PUBLISHED,
    ]

    for offset, status in enumerate(statuses, start=1):
        item = item.transition_to(status, at=NOW + timedelta(seconds=offset))
        assert item.status is status
        assert item.updated_at == NOW + timedelta(seconds=offset)


def test_same_status_is_a_noop(make_item: Callable[..., Item]) -> None:
    item = make_item()

    assert item.transition_to(ItemStatus.DISCOVERED) is item


def test_published_only_from_rendered(make_item: Callable[..., Item]) -> None:
    item = make_item(status=ItemStatus.RESOLVED)

    with pytest.raises(InvalidStateTransition):
        item.transition_to(ItemStatus.PUBLISHED)


def test_published_cannot_go_back_without_reset(make_item: Callable[..., Item]) -> None:
    item = make_item(status=ItemStatus.RENDERED)
    published = item.transition_to(ItemStatus.PUBLISHED, at=NOW + timedelta(seconds=1))

    with pytest.raises(InvalidStateTransition):
        published.transition_to(ItemStatus.OBSERVED)
    with pytest.raises(InvalidStateTransition):
        published.transition_to(ItemStatus.ACQUIRED)


def test_review_and_failure_branches(make_item: Callable[..., Item]) -> None:
    item = make_item(status=ItemStatus.OBSERVED)

    item.transition_to(ItemStatus.NEEDS_REVIEW, at=NOW + timedelta(seconds=1))
    assert item.status is ItemStatus.NEEDS_REVIEW

    item.transition_to(ItemStatus.FAILED, at=NOW + timedelta(seconds=2))
    assert item.status is ItemStatus.FAILED

    item.transition_to(ItemStatus.ARCHIVED, at=NOW + timedelta(seconds=3))
    assert item.status is ItemStatus.ARCHIVED


def test_duplicate_and_archive_branches(make_item: Callable[..., Item]) -> None:
    item = make_item(status=ItemStatus.ACQUIRED)

    item.transition_to(ItemStatus.DUPLICATE)
    assert item.status is ItemStatus.DUPLICATE

    item.transition_to(ItemStatus.ARCHIVED)
    assert item.status is ItemStatus.ARCHIVED


def test_archive_is_available_from_published(make_item: Callable[..., Item]) -> None:
    item = make_item(status=ItemStatus.PUBLISHED)

    assert item.status.can_transition_to(ItemStatus.ARCHIVED)
    assert item.transition_to(ItemStatus.ARCHIVED).status is ItemStatus.ARCHIVED


def test_invalid_status_string(make_item: Callable[..., Item]) -> None:
    with pytest.raises(DomainValidationError):
        make_item(status="bogus")


def test_invalid_transition_target(make_item: Callable[..., Item]) -> None:
    item = make_item()

    with pytest.raises(DomainValidationError):
        item.transition_to("bogus")


def test_updated_at_cannot_precede_created_at(make_item: Callable[..., Item]) -> None:
    with pytest.raises(InvalidStateTransition):
        make_item(updated_at=NOW - timedelta(seconds=1))


def test_transition_timestamp_validated(make_item: Callable[..., Item]) -> None:
    item = make_item()

    with pytest.raises(InvalidStateTransition):
        item.transition_to(ItemStatus.ACQUIRED, at=NOW - timedelta(seconds=1))


def test_allowed_transition_table_covers_every_status() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(ItemStatus)
    assert ItemStatus.PUBLISHED not in ALLOWED_TRANSITIONS[ItemStatus.PUBLISHED]


def test_direct_status_assignment_is_validated(make_item: Callable[..., Item]) -> None:
    item = make_item()

    item.status = ItemStatus.ACQUIRED
    assert item.status is ItemStatus.ACQUIRED
    assert item.updated_at is not None

    published = make_item(status=ItemStatus.PUBLISHED)
    with pytest.raises(InvalidStateTransition):
        published.status = ItemStatus.OBSERVED


def test_direct_status_assignment_rejects_invalid_value(
    make_item: Callable[..., Item],
) -> None:
    item = make_item()

    with pytest.raises(DomainValidationError):
        item.status = "bogus"  # type: ignore[assignment]


@pytest.mark.parametrize(
    "status",
    [
        ItemStatus.NEEDS_REVIEW,
        ItemStatus.FAILED,
        ItemStatus.DUPLICATE,
        ItemStatus.ARCHIVED,
    ],
)
def test_branch_is_reachable_from_active_item(
    make_item: Callable[..., Item],
    status: ItemStatus,
) -> None:
    item = make_item(status=ItemStatus.INTERPRETED)

    assert item.transition_to(status).status is status
