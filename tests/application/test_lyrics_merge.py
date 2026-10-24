"""Lyrics merge scenarios, ASR alignment, nightcore scaling, and limits."""

from __future__ import annotations

import pytest

from trackforge.application.dto.lyrics import (
    MAX_LYRICS_BYTES,
    AsrModelInfo,
    AsrSegment,
    AsrTranscript,
    AsrWord,
)
from trackforge.application.dto.providers import LyricsCandidate
from trackforge.application.use_cases.lyrics_merge import LyricsMerger

pytestmark = pytest.mark.unit


def _candidate(**overrides: object) -> LyricsCandidate:
    data: dict[str, object] = {
        "source": "lrclib",
        "title": "Song",
        "artist": "Artist",
        "duration": 200.0,
        "score": 0.9,
    }
    data.update(overrides)
    return LyricsCandidate(**data)  # type: ignore[arg-type]


def _transcript(
    *,
    text: str = "hello world",
    start: float = 1.0,
    words: bool = True,
    language: str | None = "en",
    avg_logprob: float | None = -0.2,
    no_speech_prob: float | None = 0.05,
) -> AsrTranscript:
    tokens = text.split()
    word_models = (
        [
            AsrWord(
                word=token,
                start=start + index * 0.4,
                end=start + index * 0.4 + 0.3,
                probability=0.9,
            )
            for index, token in enumerate(tokens)
        ]
        if words
        else []
    )
    return AsrTranscript(
        language=language,
        segments=[
            AsrSegment(
                start=start,
                end=start + max(0.5, len(tokens) * 0.4),
                text=text,
                words=word_models,
            )
        ],
        avg_logprob=avg_logprob,
        no_speech_prob=no_speech_prob,
        model=AsrModelInfo(id="Systran/faster-whisper-small", version="test", threads=8),
        duration_seconds=200.0,
    )


def test_valid_lrc_is_primary_and_plain_is_separate() -> None:
    result = LyricsMerger().merge(
        [
            _candidate(
                lrc="[00:01.00]First\n[00:03.00]Second\n",
                plain="First\nSecond\n",
            )
        ],
        _transcript(text="something else entirely"),
        None,
        200.0,
    )

    assert result.source == "lrclib"
    assert result.plain == "First\nSecond"
    assert result.lrc == "[00:01.00]First\n[00:03.00]Second\n"
    assert result.timing_source == "lrc"
    assert result.language == "en"
    assert result.confidence > 0.8


def test_invalid_lrc_falls_back_to_plain_and_asr() -> None:
    result = LyricsMerger().merge(
        [_candidate(lrc="not valid lrc", plain="hello world")],
        _transcript(),
        None,
        200.0,
    )

    assert "invalid_lrc" in result.warnings
    assert result.source == "plain+asr"
    assert result.plain == "hello world"
    assert result.lrc is not None and result.lrc.startswith("[00:01.00]")


def test_plain_lyrics_are_aligned_to_asr_words() -> None:
    result = LyricsMerger().merge(
        [_candidate(plain="hello world\n")],
        _transcript(),
        None,
        200.0,
    )

    assert result.source == "plain+asr"
    assert result.plain == "hello world"
    assert result.lrc is not None
    assert "[00:01.00]hello world" in result.lrc
    assert result.elrc is not None
    assert "<00:01.00>hello" in result.elrc
    assert result.timing_source == "asr_alignment"


def test_asr_only_builds_plain_lrc_and_elrc() -> None:
    result = LyricsMerger().merge([], _transcript(), None, 200.0)

    assert result.source == "asr"
    assert result.plain == "hello world"
    assert result.lrc is not None and "[00:01.00]hello world" in result.lrc
    assert result.elrc is not None
    assert result.confidence > 0.3


def test_bad_asr_keeps_good_lyrics_text_and_warns() -> None:
    result = LyricsMerger().merge(
        [_candidate(plain="hello world\n")],
        _transcript(avg_logprob=-2.5, no_speech_prob=0.9),
        None,
        200.0,
    )

    assert result.plain == "hello world"
    assert result.source == "plain+asr"
    assert "asr_low_quality" in result.warnings
    assert "low_timing_accuracy" in result.warnings
    assert result.lrc is not None


def test_nightcore_uses_original_text_and_scales_lrc_timings() -> None:
    result = LyricsMerger().merge(
        [
            _candidate(
                lrc="[00:10.00]Original line\n[00:20.00]Second line\n",
                plain="Original line\nSecond line\n",
            )
        ],
        _transcript(text="asr substitute text"),
        [{"transformation": "nightcore", "speed_factor": 1.25}],
        160.0,
    )

    assert result.plain == "Original line\nSecond line"
    assert result.lrc == "[00:08.00]Original line\n[00:16.00]Second line\n"
    assert result.source == "lrclib"


def test_nightcore_without_speed_factor_does_not_guess() -> None:
    result = LyricsMerger().merge(
        [_candidate(lrc="[00:10.00]Line\n")],
        None,
        [{"transformation": "nightcore"}],
        160.0,
    )

    assert "nightcore_speed_factor_missing" in result.warnings
    assert result.lrc == "[00:10.00]Line\n"


def test_nightcore_asr_only_does_not_replace_missing_original_silently() -> None:
    result = LyricsMerger().merge(
        [],
        _transcript(text="asr text"),
        [{"transformation": "nightcore", "speed_factor": 1.25}],
        160.0,
    )

    assert result.source == "asr"
    assert "nightcore_original_lyrics_missing" in result.warnings


def test_language_mismatch_is_visible() -> None:
    result = LyricsMerger().merge(
        [_candidate(plain="line", language="ru")],
        _transcript(language="en"),
        None,
        200.0,
    )

    assert result.language == "en"
    assert "lyrics_language_mismatch" in result.warnings


def test_oversized_plain_lyrics_are_not_embedded() -> None:
    oversized = "a" * (MAX_LYRICS_BYTES + 1)
    result = LyricsMerger().merge(
        [_candidate(plain=oversized)],
        _transcript(text="asr words"),
        None,
        200.0,
    )

    assert "lyrics_too_large" in result.warnings
    assert result.plain == "asr words"
    assert result.source == "asr"


def test_empty_transcript_and_no_lyrics_returns_unknown() -> None:
    empty = AsrTranscript(
        language=None,
        segments=[],
        model=AsrModelInfo(id="Synthetic/faster-whisper-tiny", version="test"),
        duration_seconds=10.0,
    )

    result = LyricsMerger().merge([], empty, None, 10.0)

    assert result.source == "unknown"
    assert result.plain is None
    assert "empty_transcript" in result.warnings
    assert "no_lyrics" in result.warnings


def test_mapping_candidates_are_accepted() -> None:
    result = LyricsMerger().merge(
        [
            {
                "source": "lrclib",
                "title": "Song",
                "artist": "Artist",
                "plain": "hello world",
                "score": 0.5,
            }
        ],
        _transcript(),
        None,
        200.0,
    )

    assert result.plain == "hello world"
    assert result.lrc is not None
