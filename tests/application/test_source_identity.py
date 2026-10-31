"""Unit tests for noisy source-title normalization."""

from __future__ import annotations

import pytest

from trackforge.application.services.source_identity import normalize_source_identity

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("title", "uploader", "expected_title", "expected_artist", "expected_qualifier"),
    [
        (
            "{642} Nightcore (End Of The Dream) - Collide (with lyrics)",
            "Rock Goes Nightcore",
            "Collide",
            "End Of The Dream",
            "Nightcore",
        ),
        (
            "Artist - Title (Nightcore)",
            "Some Channel",
            "Title",
            "Artist",
            "Nightcore",
        ),
        (
            "Artist - Title (Lyrics)",
            "Some Channel",
            "Title",
            "Artist",
            None,
        ),
        (
            "Artist - Title (Official Video) [HD]",
            "Some Channel",
            "Title",
            "Artist",
            None,
        ),
        (
            "Rock Goes Nightcore - End Of The Dream - Collide",
            "Rock Goes Nightcore",
            "Collide",
            "End Of The Dream",
            "Nightcore",
        ),
        (
            "[7] Artist - Title [4K]",
            "Some Channel",
            "Title",
            "Artist",
            None,
        ),
        (
            "Nightcore - Artist - Title (Sped Up)",
            "Nightcore",
            "Title",
            "Artist",
            "Nightcore",
        ),
    ],
)
def test_normalizes_observed_source_titles(
    title: str,
    uploader: str,
    expected_title: str,
    expected_artist: str,
    expected_qualifier: str | None,
) -> None:
    identity = normalize_source_identity(title, uploader, None)

    assert identity.title == expected_title
    assert identity.artist == expected_artist
    assert identity.qualifier == expected_qualifier
    assert identity.source_title == title
    assert identity.confidence >= 0.7


def test_original_title_on_fifth_description_line_wins() -> None:
    description = "\n".join(
        [
            "Synthetic description line one",
            "Synthetic description line two",
            "Synthetic description line three",
            "Synthetic description line four",
            "Original title: Another Artist - Another Song",
            "Follow",
        ]
    )
    identity = normalize_source_identity(
        "{99} Nightcore (Some Uploader) - Wrong Song (with lyrics)",
        "Some Uploader",
        description,
    )

    assert identity.title == "Another Song"
    assert identity.artist == "Another Artist"
    assert identity.qualifier == "Nightcore"
    assert identity.confidence >= 0.7


def test_original_title_without_artist_uses_description_artist() -> None:
    identity = normalize_source_identity(
        "Noisy upload title",
        "Curator Channel",
        "Original title: Clean Song\nOriginal artist: Real Artist",
    )

    assert identity.title == "Clean Song"
    assert identity.artist == "Real Artist"


def test_missing_original_artist_stays_unknown_for_derivative() -> None:
    identity = normalize_source_identity(
        "{42} Nightcore - Noisy Song (with lyrics)",
        "Nightcore Fan Channel",
        None,
    )

    assert identity.title == "Noisy Song"
    assert identity.artist is None
    assert identity.qualifier == "Nightcore"
    assert "original_artist_unknown" in identity.warnings


def test_clean_non_derivative_title_can_use_uploader_as_artist() -> None:
    identity = normalize_source_identity("Plain Song", "Plain Artist", None)

    assert identity.title == "Plain Song"
    assert identity.artist == "Plain Artist"
    assert identity.qualifier is None


def test_parenthetical_artist_after_title_is_extracted() -> None:
    identity = normalize_source_identity(
        "Nightcore - Gone Forever (Three Days Grace) - (Lyrics)",
        "Cherry 葵",
        None,
    )

    assert identity.title == "Gone Forever"
    assert identity.artist == "Three Days Grace"


def test_bracket_artist_after_title_is_extracted() -> None:
    identity = normalize_source_identity(
        "Nightcore - Feel Invincible [Skillet]",
        "ZeroMizKun Nightcore",
        None,
    )

    assert identity.title == "Feel Invincible"
    assert identity.artist == "Skillet"


def test_support_original_artists_header_on_next_line() -> None:
    identity = normalize_source_identity(
        "Nightcore - Protector",
        "Gyuuki Nightcore",
        "NIGHTCORE DETAILS\n♫ Original Song Title: City Wolf - Protector\n"
        "Support the Original Artist(s):\nCity Wolf\n"
        "- https://example.invalid/artist",
    )

    assert identity.title == "Protector"
    assert identity.artist == "City Wolf"


def test_artist_label_and_original_by_patterns() -> None:
    labelled = normalize_source_identity(
        "Nightcore - Straight For Disaster",
        "Cherry 葵",
        "Artist(s): Cold Driven\nSupport the Original Artist(s):\n~ Cold Driven",
    )
    original_by = normalize_source_identity(
        "Nightcore - Somewhere I Belong (Rock Cover)",
        "Cherry 葵",
        "Original by: Linkin Park\nSupport the Original Artist(s):\nALESTI",
    )
    original_artists = normalize_source_identity(
        "Nightcore - Explode",
        "Gyuuki Nightcore",
        "NIGHTCORE DETAILS\n♫ Original Song Title: Written By Wolves - Explode\n"
        "ORIGINAL ARTISTS - Written By Wolves",
    )

    assert labelled.artist == "Cold Driven"
    assert original_by.artist == "Linkin Park"
    assert original_artists.artist == "Written By Wolves"
