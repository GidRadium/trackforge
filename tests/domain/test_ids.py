"""UUIDv7 and ID-boundary tests."""

from __future__ import annotations

import uuid

import pytest

from trackforge.domain.errors import DomainValidationError, InvalidId
from trackforge.domain.model.ids import ItemId, MediaId, new_uuid7, parse_id


def _timestamp_ms(value: str) -> int:
    return int(uuid.UUID(value).hex[:12], 16)


def test_uuid7_version_and_variant() -> None:
    parsed = uuid.UUID(new_uuid7())

    assert parsed.version == 7
    assert parsed.variant == uuid.RFC_4122


def test_uuid7_is_canonical_text() -> None:
    value = new_uuid7()

    assert str(uuid.UUID(value)) == value


def test_uuid7_time_prefix_is_monotonic() -> None:
    values = [new_uuid7() for _ in range(1000)]

    assert values == sorted(values)
    timestamps = [_timestamp_ms(value) for value in values]
    assert timestamps == sorted(timestamps)


def test_uuid7_explicit_timestamp_is_used() -> None:
    now_ns = 1_700_000_000_123_456_789

    value = new_uuid7(now_ns=now_ns)

    assert _timestamp_ms(value) == now_ns // 1_000_000


def test_uuid7_same_millisecond_stays_sorted() -> None:
    values = [new_uuid7(now_ns=1_700_000_000_000_000_000) for _ in range(50)]

    assert values == sorted(values)
    assert len(set(values)) == len(values)


def test_uuid7_rejects_negative_time() -> None:
    with pytest.raises(DomainValidationError):
        new_uuid7(now_ns=-1)


def test_uuid7_rejects_non_integer_time() -> None:
    with pytest.raises(DomainValidationError):
        new_uuid7(now_ns=1.5)  # type: ignore[arg-type]


def test_parse_id_accepts_uuid_versions() -> None:
    value = str(uuid.uuid4())

    assert parse_id(value, ItemId) == value


def test_parse_id_returns_canonical_text() -> None:
    value = str(uuid.uuid4()).upper()

    assert parse_id(value, ItemId) == value.lower()


@pytest.mark.parametrize("value", ["", "not-a-uuid", "12345", "urn:not-a-uuid"])
def test_parse_id_rejects_invalid_values(value: str) -> None:
    with pytest.raises(InvalidId) as excinfo:
        parse_id(value, ItemId)

    assert excinfo.value.context["kind"] == "ItemId"


def test_parse_id_rejects_non_string() -> None:
    with pytest.raises(InvalidId):
        parse_id(123, ItemId)  # type: ignore[arg-type]


def test_ids_are_opaque_not_content_hashes() -> None:
    first = new_uuid7()
    second = new_uuid7()

    assert first != second
    assert parse_id(first, MediaId) == first
