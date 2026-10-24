"""Normalization tests for comparison-safe text, artists, dates, BPM, and keys."""

from __future__ import annotations

import pytest

from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model.identity import Transformation
from trackforge.domain.policies.normalization import (
    apply_qualifier,
    canonical_field,
    comparison_key,
    detect_source_noise,
    detect_transformations,
    display_qualifier,
    is_vevo,
    normalize_artist_name,
    normalize_bpm,
    normalize_date,
    normalize_key,
    normalize_text,
    parse_artists,
    parse_date,
    rank_transformations,
    strip_topic_suffix,
    title_has_qualifier,
)


def test_normalize_text_applies_nfkc_and_collapses_whitespace() -> None:
    assert normalize_text("  Ｓｏｎｇ\u00a0  Mix  ") == "Song Mix"
    assert normalize_text("Don’t Stop") == "Don't Stop"


def test_comparison_key_casefolds_and_normalizes_separators() -> None:
    assert comparison_key("Song!") == comparison_key("song")
    assert comparison_key("A feat. B") == comparison_key("A featuring B")
    assert comparison_key("A & B") == comparison_key("A and B")
    assert comparison_key("A vs. B") == comparison_key("A versus B")


def test_comparison_key_preserves_qualifier_content() -> None:
    assert comparison_key("Song (Remastered 2011)") != comparison_key("Song")


def test_topic_and_vevo_are_source_noise_not_artists() -> None:
    assert strip_topic_suffix("Artist - Topic") == "Artist"
    assert normalize_artist_name("Artist - Topic") == "Artist"
    assert is_vevo("ArtistVEVO")
    assert is_vevo("VEVO")
    assert normalize_artist_name("VEVO") == ""
    assert normalize_artist_name("ArtistVEVO") == ""


def test_parse_artists_splits_roles_and_separators() -> None:
    credits = parse_artists("Artist A, Artist B & Artist C feat. Artist D x Artist E")

    assert [(credit.name, credit.role) for credit in credits] == [
        ("Artist A", "main"),
        ("Artist B", "main"),
        ("Artist C", "main"),
        ("Artist D", "featured"),
        ("Artist E", "featured"),
    ]


def test_parse_artists_keeps_ampersand_names_but_uses_heuristic() -> None:
    credits = parse_artists("Simon & Garfunkel")

    assert [credit.name for credit in credits] == ["Simon", "Garfunkel"]


def test_source_noise_is_marked_and_not_removed() -> None:
    title = "Song (Official Video) [4K]"

    assert detect_source_noise(title) == ("official_video", "4k")
    assert normalize_text(title) == title


def test_apply_qualifier_does_not_duplicate() -> None:
    assert apply_qualifier("Song", Transformation.NIGHTCORE) == "Song (Nightcore)"
    assert apply_qualifier("Song (Nightcore)", Transformation.NIGHTCORE) == "Song (Nightcore)"
    assert apply_qualifier("Song (nightcore)", Transformation.NIGHTCORE) == "Song (nightcore)"
    assert title_has_qualifier("Song (Nightcore)", Transformation.NIGHTCORE)


def test_detect_transformations_and_priority() -> None:
    detected = detect_transformations("Song (Nightcore) [Sped Up + Reverb]")
    assert set(detected) == {
        Transformation.NIGHTCORE,
        Transformation.SPED_UP,
        Transformation.REVERB,
    }
    assert rank_transformations(detected)[0] is Transformation.NIGHTCORE


def test_display_qualifier() -> None:
    assert display_qualifier(Transformation.SPED_UP) == "Sped Up"
    assert display_qualifier("8d") == "8D"


def test_date_normalization_preserves_precision() -> None:
    assert normalize_date("2024") == "2024"
    assert normalize_date("2024-5") == "2024-05"
    assert normalize_date("2024/05/02") == "2024-05-02"

    parsed = parse_date("2024-05")
    assert parsed is not None
    assert parsed.as_date() is None
    assert parsed.value == "2024-05"


@pytest.mark.parametrize(
    "value",
    ["", "24", "2024-13", "2024-02-30", "2024-5-1-2", "not-a-date", "2024-05-XX"],
)
def test_date_normalization_never_guesses_or_accepts_invalid(value: str) -> None:
    assert normalize_date(value) is None


def test_bpm_keeps_half_and_double_values() -> None:
    assert normalize_bpm("85") == 85.0
    assert normalize_bpm(170) == 170.0
    assert normalize_bpm("85,5") == 85.5
    assert normalize_bpm("-1") is None
    assert normalize_bpm("fast") is None


def test_key_normalization() -> None:
    assert normalize_key("C") == "C"
    assert normalize_key("c#") == "C#"
    assert normalize_key("D♭") == "Db"
    assert normalize_key("A minor") == "Am"
    assert normalize_key("F#m") == "F#m"
    assert normalize_key("H") is None


def test_canonical_field_aliases() -> None:
    assert canonical_field("cover") == "artwork"
    assert canonical_field("album_artist") == "albumartist"
    assert canonical_field("track_total") == "tracktotal"
    with pytest.raises(DomainValidationError):
        canonical_field("")


def test_strip_qualifier_normalizes_prefix_and_suffix() -> None:
    from trackforge.domain.policies.normalization import strip_qualifier

    assert strip_qualifier("Nightcore | Queen of Shadow", Transformation.NIGHTCORE) == (
        "Queen of Shadow"
    )
    assert strip_qualifier("Queen of Shadow (Nightcore)", Transformation.NIGHTCORE) == (
        "Queen of Shadow"
    )
    assert strip_qualifier("Queen of Shadow - Nightcore", Transformation.NIGHTCORE) == (
        "Queen of Shadow"
    )
    assert strip_qualifier("Nightcore", Transformation.NIGHTCORE) == "Nightcore"
