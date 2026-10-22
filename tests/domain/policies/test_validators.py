"""Field and cross-field validator tests."""

from __future__ import annotations

from datetime import date

import pytest

from trackforge.domain.policies.validators import (
    Severity,
    merge_results,
    validate_claim,
    validate_confidence,
    validate_cross_fields,
    validate_field,
    validate_values,
)


@pytest.mark.parametrize("field", ["title", "artist", "album"])
def test_empty_required_text_is_hard_invalid(field: str) -> None:
    result = validate_field(field, "   ")

    assert result.valid is False
    assert result.hard_issues[0].code == "empty_value"


@pytest.mark.parametrize("field", ["title", "artist"])
def test_path_separators_are_hard_invalid(field: str) -> None:
    result = validate_field(field, "Artist/Song")

    assert result.valid is False
    assert result.hard_issues[0].code == "path_separator"


def test_suspicious_length_is_only_a_warning() -> None:
    result = validate_field("title", "x" * 301)

    assert result.valid is True
    assert result.hard_issues == ()
    assert result.soft_issues[0].severity is Severity.SOFT
    assert "suspicious" in result.warnings[0]


def test_valid_mbid_and_isrc() -> None:
    assert validate_field("mb_recording_id", "11111111-1111-1111-1111-111111111111").valid
    assert validate_field("isrc", "US-ABC-24-12345").valid
    assert not validate_field("mb_recording_id", "not-a-uuid").valid
    assert not validate_field("isrc", "US-ABC-24-1234").valid


def test_bpm_range_and_type() -> None:
    assert validate_field("bpm", 85).valid
    assert validate_field("bpm", 170.0).valid
    assert not validate_field("bpm", 10).valid
    assert not validate_field("bpm", 400).valid
    assert not validate_field("bpm", "fast").valid


def test_key_validation() -> None:
    assert validate_field("key", "F#m").valid
    assert validate_field("key", "Db").valid
    assert not validate_field("key", "H").valid


@pytest.mark.parametrize("field", ["track", "disc", "tracktotal", "disctotal"])
def test_zero_or_negative_track_and_disc_are_hard_invalid(field: str) -> None:
    assert validate_field(field, 1).valid
    assert not validate_field(field, 0).valid
    assert not validate_field(field, -1).valid


def test_future_date_is_hard_invalid_unless_announced() -> None:
    today = date(2025, 1, 1)

    assert validate_field("date", "2024-12-31", today=today).valid
    future = validate_field("date", "2025-01-02", today=today)
    assert future.valid is False
    assert future.hard_issues[0].code == "future_date"
    assert validate_field("date", "2025-01-02", allow_future=True, today=today).valid


def test_invalid_date_is_hard_invalid() -> None:
    result = validate_field("date", "2024-13")

    assert result.valid is False
    assert result.hard_issues[0].code == "invalid_date"


def test_confidence_range() -> None:
    assert validate_confidence(0.5).valid
    assert not validate_confidence(1.5).valid
    assert not validate_confidence(-0.1).valid
    assert not validate_confidence("high").valid


def test_track_exceeds_total_is_hard() -> None:
    result = validate_cross_fields({"track": 5, "tracktotal": 3})

    assert result.valid is False
    assert result.hard_issues[0].code == "track_exceeds_total"


def test_disc_exceeds_total_is_hard() -> None:
    result = validate_cross_fields({"disc": 3, "disctotal": 2})

    assert result.valid is False
    assert result.hard_issues[0].code == "disc_exceeds_total"


def test_date_before_originaldate_is_hard() -> None:
    result = validate_cross_fields({"date": "2020-01-01", "originaldate": "2021-01-01"})

    assert result.valid is False
    assert result.hard_issues[0].code == "date_before_originaldate"


def test_album_without_albumartist_is_a_warning() -> None:
    result = validate_cross_fields({"album": "Album"})

    assert result.valid is True
    assert result.soft_issues[0].code == "albumartist_missing"


def test_compilation_consistency_is_a_warning() -> None:
    result = validate_cross_fields(
        {"album": "Album", "albumartist": "Solo Artist", "compilation": True}
    )

    assert result.valid is True
    assert "compilation_albumartist_mismatch" in result.codes


def test_merge_results_deduplicates() -> None:
    first = validate_field("title", "")
    second = validate_field("title", "")

    merged = merge_results(first, second)

    assert len(merged.issues) == 1


def test_validate_values_combines_fields_and_cross_fields() -> None:
    result = validate_values({"title": "Song", "album": "Album", "track": 4, "tracktotal": 3})

    assert result.valid is False
    assert {issue.field for issue in result.issues} == {"albumartist", "track"}


def test_claim_alias() -> None:
    assert validate_claim("artist", "Artist").valid
