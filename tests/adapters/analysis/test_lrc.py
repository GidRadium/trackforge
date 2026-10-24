"""LRC/ELRC parsing, validation, and round-trip tests."""

from __future__ import annotations

import pytest

from trackforge.application.dto.lyrics import (
    AsrSegment,
    AsrWord,
    LrcDocument,
    LrcLine,
    LrcMetadata,
)
from trackforge.application.services.lrc import (
    LrcParseError,
    format_lrc_timestamp,
    is_valid_lrc,
    parse_lrc,
    render_elrc,
    render_lrc,
    try_parse_lrc,
)

pytestmark = pytest.mark.unit

SAMPLE = """[ti:Song]
[ar:Artist]
[al:Album]
[by:someone]
[offset:-120]
[00:01.00]First line
[00:03.50]Second line
[00:06.250]Third line
"""


def test_parse_metadata_and_lines() -> None:
    document = parse_lrc(SAMPLE)

    assert document.metadata == LrcMetadata(
        title="Song",
        artist="Artist",
        album="Album",
        by="someone",
        offset_ms=-120,
    )
    assert [line.timestamp_ms for line in document.lines] == [1000, 3500, 6250]
    assert [line.text for line in document.lines] == ["First line", "Second line", "Third line"]


def test_render_round_trip_is_stable() -> None:
    document = parse_lrc(SAMPLE)

    rendered = render_lrc(document)

    assert rendered.startswith("[ti:Song]\n[ar:Artist]\n")
    assert rendered.endswith("[00:06.25]Third line\n")
    assert parse_lrc(rendered) == document
    assert render_lrc(parse_lrc(rendered)) == rendered


def test_multiple_timestamps_and_empty_lines_are_supported() -> None:
    document = parse_lrc("[00:01.00][00:02.00]Chorus\n[00:03.00]\n[00:04.00]End\n")

    assert [line.timestamp_ms for line in document.lines] == [1000, 2000, 3000, 4000]
    assert [line.text for line in document.lines] == ["Chorus", "Chorus", "", "End"]


def test_non_monotonic_timestamps_are_rejected() -> None:
    with pytest.raises(LrcParseError, match="monotonic"):
        parse_lrc("[00:05.00]Later\n[00:01.00]Earlier\n")


@pytest.mark.parametrize(
    "text",
    [
        "[0:01.00]bad minutes\n",
        "[00:60.00]bad seconds\n",
        "[00:01]missing fraction\n",
        "plain text without a timestamp\n",
        "[00:01.00]ok\n[offset:abc]\n",
    ],
)
def test_invalid_timing_is_rejected(text: str) -> None:
    with pytest.raises(LrcParseError):
        parse_lrc(text)


def test_try_parse_returns_warning_for_invalid_lrc() -> None:
    document, warnings = try_parse_lrc("not lrc")

    assert document is None
    assert warnings == ("invalid_lrc",)
    assert not is_valid_lrc("not lrc")


def test_timestamp_formatting() -> None:
    assert format_lrc_timestamp(0) == "[00:00.00]"
    assert format_lrc_timestamp(61_234) == "[01:01.234]"
    assert format_lrc_timestamp(61_230) == "[01:01.23]"
    assert format_lrc_timestamp(6_000_000) == "[100:00.00]"


def test_render_elrc_contains_word_timestamps() -> None:
    segments = [
        AsrSegment(
            start=1.0,
            end=2.5,
            text="hello world",
            words=[
                AsrWord(word="hello", start=1.0, end=1.4, probability=0.9),
                AsrWord(word="world", start=1.5, end=2.0, probability=0.8),
            ],
        )
    ]

    rendered = render_elrc(segments)

    assert rendered.startswith("[00:01.00]<00:01.00>hello <00:01.50>world")
    assert rendered.endswith("\n")


def test_render_elrc_skips_segments_without_words() -> None:
    segments = [AsrSegment(start=1.0, end=2.0, text="no words")]

    assert render_elrc(segments) == ""


def test_document_rejects_non_monotonic_construction() -> None:
    with pytest.raises(Exception, match="monotonic"):
        LrcDocument(lines=(LrcLine(2000, "b"), LrcLine(1000, "a")))
