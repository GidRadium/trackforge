"""Unit tests for explicit description lyrics extraction."""

from __future__ import annotations

import pytest

from trackforge.application.services.source_lyrics import (
    MAX_DESCRIPTION_LYRICS_BYTES,
    extract_description_lyrics,
)

pytestmark = pytest.mark.unit

SYNTHETIC_LYRICS = "\n".join(
    f"synthetic lyric line {index} with enough words for a fixture" for index in range(6)
)


def test_explicit_lyrics_section_is_captured_without_footer() -> None:
    description = (
        f"Intro text\nLyrics:\n{SYNTHETIC_LYRICS}\nFollow\nOriginal art: synthetic\nMore videos"
    )

    result = extract_description_lyrics(description)

    assert result is not None
    assert result.text == SYNTHETIC_LYRICS
    assert result.confidence == pytest.approx(0.55)
    assert "Follow" not in result.text
    assert "Original art" not in result.text


def test_lyrics_header_without_colon_is_supported() -> None:
    result = extract_description_lyrics(f"Intro\nLyrics\n{SYNTHETIC_LYRICS}\nNightcore mixes")

    assert result is not None
    assert result.text == SYNTHETIC_LYRICS


def test_lyric_singular_and_inline_text_are_supported() -> None:
    result = extract_description_lyrics(
        f"Lyric: {SYNTHETIC_LYRICS}\nBusiness: fixture@example.test"
    )

    assert result is not None
    assert result.text == SYNTHETIC_LYRICS


def test_section_without_marker_is_ignored() -> None:
    assert extract_description_lyrics(SYNTHETIC_LYRICS) is None
    assert extract_description_lyrics(None) is None


def test_timed_block_is_skipped_instead_of_becoming_plain_lyrics() -> None:
    result = extract_description_lyrics(
        "Lyrics:\n[00:10.00] synthetic line one\n[00:20.00] synthetic line two\n"
    )

    assert result is not None
    assert result.text is None
    assert "description_lyrics_synced_skipped" in result.warnings


def test_short_and_url_only_sections_are_skipped() -> None:
    short = extract_description_lyrics("Lyrics:\nway too short")
    urls = extract_description_lyrics(
        "Lyrics:\nhttps://example.test/one\nhttps://example.test/two\n"
    )

    assert short is not None and short.text is None
    assert "description_lyrics_too_short" in short.warnings
    assert urls is not None and urls.text is None
    assert "description_lyrics_url_only" in urls.warnings


def test_oversized_section_is_truncated_at_the_byte_limit() -> None:
    block = "\n".join(f"synthetic line {index} " + "x" * 90 for index in range(4000))
    result = extract_description_lyrics(f"Lyrics:\n{block}")

    assert result is not None
    assert result.text is not None
    assert len(result.text.encode("utf-8")) <= MAX_DESCRIPTION_LYRICS_BYTES
    assert "description_lyrics_truncated" in result.warnings
