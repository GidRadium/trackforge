"""Unit tests for source genre hygiene."""

from __future__ import annotations

import pytest

from trackforge.application.services.source_genres import normalize_source_genres

pytestmark = pytest.mark.unit


def test_drops_source_categories_and_limits_to_six() -> None:
    tags = [
        "Music",
        "Lyrics",
        "Lyrics (Website Category)",
        "Drama (TV Genre)",
        "Comedy (Film Genre)",
        "https://example.test/tag",
        "Rock",
        "rock",
        "Electronic",
        "Synthpop",
        "Darkwave",
        "Industrial",
        "Ambient",
        "Extra Seven",
    ]

    result = normalize_source_genres(tags, "Collide", "Rock Goes Nightcore")

    assert result == ("Rock", "Electronic", "Synthpop", "Darkwave", "Industrial", "Ambient")
    assert len(result) == 6


def test_drops_uploader_title_fragments_and_overlong_values() -> None:
    result = normalize_source_genres(
        ["Rock Goes Nightcore", "Collide", "x" * 41, "Synthwave"],
        "Collide",
        "Rock Goes Nightcore",
    )

    assert result == ("Synthwave",)


def test_accepts_a_single_string_and_none() -> None:
    assert normalize_source_genres("Rock", None, None) == ("Rock",)
    assert normalize_source_genres(None, None, None) == ()
