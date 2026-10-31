"""Merge lyrics providers, ASR transcripts, and derivative timing evidence.

The merger is deliberately conservative: it never rewrites the meaning of a
lyric, never invents timings without a warning, and never replaces a found
original lyric with noisier ASR text.  Five scenarios are covered:

1. valid LRC with a consistent duration/score;
2. plain lyrics aligned to ASR words;
3. ASR-only text/timings;
4. noisy ASR with good lyrics (text from lyrics, timings from ASR);
5. nightcore/sped-up originals with timings scaled by ``speed_factor``.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from trackforge.application.dto.lyrics import (
    MAX_LYRICS_BYTES,
    AsrSegment,
    AsrTranscript,
    AsrWord,
    LrcDocument,
    LrcLine,
    LyricsResult,
    lyrics_size_bytes,
    normalize_lyrics_text,
    valid_language,
)
from trackforge.application.dto.providers import LyricsCandidate
from trackforge.application.services.lrc import (
    lrc_plain_text,
    render_elrc,
    render_lrc,
    try_parse_lrc,
)
from trackforge.domain.model.claims import Claim, ClaimStatus, Subject

_NIGHTCORE_TRANSFORMS = frozenset({"nightcore", "sped_up", "spedup", "speed_up", "speedup"})
_TRANSFORM_FIELDS = frozenset({"transformation", "transformations"})
_SPEED_FIELDS = frozenset({"speed_factor", "speed", "tempo_factor", "speed_ratio"})
_TOKEN_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)
_ACTIVE_STATUSES = frozenset({ClaimStatus.PROPOSED, ClaimStatus.ACCEPTED})
_LOW_QUALITY_LOGPROB = -1.0
_HIGH_NO_SPEECH = 0.6
_LOW_WORD_PROBABILITY = 0.35


@dataclass(frozen=True, slots=True)
class _Candidate:
    source: str
    title: str
    artist: str
    plain: str | None
    lrc: str | None
    language: str | None
    score: float
    duration: float | None
    has_timing: bool


@dataclass(frozen=True, slots=True)
class _Aligned:
    lines: tuple[LrcLine, ...]
    segments: tuple[AsrSegment, ...]
    coverage: float


class LyricsMerger:
    """Pure merge of provider candidates and one optional ASR transcript."""

    def merge(
        self,
        lyrics_candidates: Sequence[LyricsCandidate | Mapping[str, Any]] | None,
        asr_transcript: AsrTranscript | None,
        derivative_claims: Sequence[Claim] | Sequence[str] | Mapping[str, Any] | None = None,
        duration: float | None = None,
        *,
        title: str | None = None,
        artist: str | None = None,
    ) -> LyricsResult:
        warnings: list[str] = []

        candidates = [
            candidate
            for candidate in (_coerce_candidate(item) for item in (lyrics_candidates or ()))
            if candidate is not None
        ]

        transformations, speed_factor = _derivative_parameters(derivative_claims)
        nightcore = bool(_NIGHTCORE_TRANSFORMS.intersection(transformations))
        if nightcore:
            if speed_factor is None:
                warnings.append("nightcore_speed_factor_missing")
            elif not math.isfinite(speed_factor) or speed_factor <= 0:
                warnings.append("invalid_speed_factor")
                speed_factor = None

        lrc_candidate, lrc_document = _best_lrc(
            candidates,
            duration,
            title,
            artist,
            warnings,
        )
        plain_candidate = _best_plain(candidates, duration, title, artist, warnings)
        asr_quality = _asr_quality(asr_transcript)

        language = _merge_language(asr_transcript, lrc_candidate, plain_candidate, warnings)
        if (
            nightcore
            and speed_factor is None
            and duration is not None
            and duration > 0
            and lrc_document is not None
            and lrc_document.lines
        ):
            # LRCLIB often returns the original-tempo timings while the audio
            # is a nightcore speed-up.  The last lyric line gives a safe
            # lower-bound duration of the original song; only use it when the
            # LRC clearly extends past the measured derivative audio.
            last_ms = max(line.timestamp_ms for line in lrc_document.lines)
            measured_ms = duration * 1000.0
            if measured_ms > 0 and last_ms > measured_ms * 1.02:
                inferred = last_ms / measured_ms
                if 1.02 <= inferred <= 2.5:
                    speed_factor = inferred
                    warnings = [
                        item for item in warnings if item != "nightcore_speed_factor_missing"
                    ]
                    warnings.append("nightcore_speed_factor_inferred_from_lrc")
        timing_scale = (1.0 / speed_factor) if nightcore and speed_factor else None
        plain: str | None = None

        if lrc_document is not None and lrc_candidate is not None:
            document = lrc_document
            if timing_scale is not None:
                document = _scale_document(document, timing_scale)
            if (
                lrc_candidate.plain is not None
                and lyrics_size_bytes(lrc_candidate.plain) > MAX_LYRICS_BYTES
            ):
                warnings.append("lyrics_too_large")
            plain = _candidate_plain(lrc_candidate) or lrc_plain_text(document)
            if not plain and not document.lines:
                plain = None
            if asr_quality == "bad":
                warnings.append("asr_low_quality")
            if not document.lines:
                lrc_text = None
            else:
                lrc_text = render_lrc(document)
            _check_size(plain, lrc_text, None, warnings)
            return LyricsResult(
                plain=plain,
                lrc=lrc_text,
                source=lrc_candidate.source,
                confidence=_clamp(0.62 + 0.3 * lrc_candidate.score),
                warnings=tuple(warnings),
                language=language,
                timing_source="lrc",
            )

        if plain_candidate is not None:
            plain = _candidate_plain(plain_candidate)
            if asr_transcript is None or asr_transcript.empty:
                warnings.append("asr_unavailable_for_alignment")
                _check_size(plain, None, None, warnings)
                return LyricsResult(
                    plain=plain,
                    source=plain_candidate.source,
                    confidence=_clamp(0.35 + 0.2 * plain_candidate.score),
                    warnings=tuple(warnings),
                    language=language,
                )
            if asr_quality == "bad":
                warnings.append("asr_low_quality")
            aligned = _align_plain_to_asr(plain or "", asr_transcript, duration)
            if aligned is None:
                document = _proportional_document(plain or "", duration, asr_transcript)
                warnings.append("alignment_unmatched")
                coverage = 0.0
                elrc = None
            else:
                document = LrcDocument(lines=aligned.lines)
                coverage = aligned.coverage
                elrc = render_elrc(aligned.segments) if aligned.segments else None
                if coverage < 0.3:
                    warnings.append("low_timing_accuracy")
                    elrc = None
            if asr_quality == "bad":
                document = _proportional_document(plain or "", duration, asr_transcript)
                elrc = None
                warnings.append("low_timing_accuracy")
            lrc_text = render_lrc(document) if document.lines else None
            _check_size(plain, lrc_text, elrc, warnings)
            confidence = _clamp(
                0.4
                + 0.3 * coverage
                + 0.15 * plain_candidate.score
                - (0.15 if asr_quality == "bad" else 0.0)
            )
            return LyricsResult(
                plain=plain,
                lrc=lrc_text,
                elrc=elrc,
                source="plain+asr",
                confidence=confidence,
                warnings=tuple(warnings),
                language=language,
                timing_source="asr_alignment",
            )

        if asr_transcript is not None and not asr_transcript.empty:
            plain = _limited(asr_transcript.plain_text(), warnings)
            if asr_quality == "bad":
                warnings.append("asr_low_quality")
            document = _asr_document(asr_transcript)
            elrc = _asr_elrc(asr_transcript)
            if nightcore:
                # ASR was run on derivative audio, so its timings already
                # match the output.  Text is the best available fallback when
                # no original lyrics exist.
                warnings.append("nightcore_original_lyrics_missing")
            lrc_text = render_lrc(document) if document.lines else None
            _check_size(plain, lrc_text, elrc, warnings)
            return LyricsResult(
                plain=plain,
                lrc=lrc_text,
                elrc=elrc,
                source="asr",
                confidence=_asr_confidence(asr_transcript, asr_quality),
                warnings=tuple(warnings),
                language=language,
                timing_source="asr",
            )

        if asr_transcript is not None and asr_transcript.empty:
            warnings.append("empty_transcript")
        warnings.append("no_lyrics")
        return LyricsResult(
            source="unknown",
            confidence=0.0,
            warnings=tuple(warnings),
            language=language,
        )


def _coerce_candidate(value: object) -> _Candidate | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        get = value.get
        source = str(get("source") or "").strip()
        title = str(get("title") or "").strip()
        artist = str(get("artist") or "").strip()
        plain = get("plain")
        lrc = get("lrc")
        language = get("language")
        score = get("score", 0.0)
        duration = get("duration")
        has_timing = bool(get("has_timing", False))
    else:
        source = str(getattr(value, "source", "") or "").strip()
        title = str(getattr(value, "title", "") or "").strip()
        artist = str(getattr(value, "artist", "") or "").strip()
        plain = getattr(value, "plain", None)
        lrc = getattr(value, "lrc", None)
        language = getattr(value, "language", None)
        score = getattr(value, "score", 0.0)
        duration = getattr(value, "duration", None)
        has_timing = bool(getattr(value, "has_timing", False))
    if not source or not title or not artist:
        return None
    try:
        numeric_score = float(score)
    except (TypeError, ValueError):
        numeric_score = 0.0
    numeric_duration: float | None
    try:
        numeric_duration = None if duration is None else float(duration)
    except (TypeError, ValueError):
        numeric_duration = None
    return _Candidate(
        source=source,
        title=title,
        artist=artist,
        plain=_text_or_none(plain),
        lrc=_text_or_none(lrc),
        language=valid_language(language if isinstance(language, str) else None),
        score=_clamp(numeric_score),
        duration=numeric_duration if numeric_duration is None or numeric_duration >= 0 else None,
        has_timing=has_timing,
    )


def _text_or_none(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = normalize_lyrics_text(value)
    return text or None


def _candidate_plain(candidate: _Candidate) -> str | None:
    plain = candidate.plain
    if plain is None:
        return None
    if lyrics_size_bytes(plain) > MAX_LYRICS_BYTES:
        return None
    return plain


def _duration_match(candidate: _Candidate, duration: float | None) -> bool:
    if candidate.duration is None or duration is None or duration <= 0:
        return True
    tolerance = max(5.0, duration * 0.04)
    return abs(candidate.duration - duration) <= tolerance


def _metadata_match(
    candidate: _Candidate,
    title: str | None,
    artist: str | None,
) -> float:
    score = 0.0
    if title:
        score += _similarity(candidate.title, title) * 0.6
    if artist:
        score += _similarity(candidate.artist, artist) * 0.4
    return score


def _candidate_rank(
    candidate: _Candidate,
    duration: float | None,
    title: str | None,
    artist: str | None,
) -> tuple[float, float, int]:
    match = _metadata_match(candidate, title, artist)
    duration_bonus = 1.0 if _duration_match(candidate, duration) else 0.0
    return (duration_bonus + match, candidate.score, len(candidate.lrc or ""))


def _best_lrc(
    candidates: Sequence[_Candidate],
    duration: float | None,
    title: str | None,
    artist: str | None,
    warnings: list[str],
) -> tuple[_Candidate | None, LrcDocument | None]:
    parsed: list[tuple[_Candidate, LrcDocument]] = []
    invalid_sources: set[str] = set()
    for candidate in candidates:
        if not candidate.lrc:
            continue
        document, parse_warnings = try_parse_lrc(candidate.lrc)
        if document is None:
            invalid_sources.add(candidate.source)
            continue
        if not _duration_match(candidate, duration):
            warnings.append("lyrics_duration_mismatch")
        parsed.append((candidate, document))
    if invalid_sources:
        warnings.append("invalid_lrc")
    if not parsed:
        return None, None
    candidate, document = max(
        parsed,
        key=lambda item: _candidate_rank(item[0], duration, title, artist),
    )
    return candidate, document


def _best_plain(
    candidates: Sequence[_Candidate],
    duration: float | None,
    title: str | None,
    artist: str | None,
    warnings: list[str],
) -> _Candidate | None:
    plain_candidates = []
    for candidate in candidates:
        if not candidate.plain:
            continue
        if lyrics_size_bytes(candidate.plain) > MAX_LYRICS_BYTES:
            warnings.append("lyrics_too_large")
            continue
        plain_candidates.append(candidate)
    if not plain_candidates:
        return None
    return max(
        plain_candidates,
        key=lambda item: _candidate_rank(item, duration, title, artist),
    )


def _merge_language(
    asr: AsrTranscript | None,
    lrc_candidate: _Candidate | None,
    plain_candidate: _Candidate | None,
    warnings: list[str],
) -> str | None:
    asr_language = valid_language(asr.language) if asr is not None else None
    lyric_language = None
    for candidate in (lrc_candidate, plain_candidate):
        if candidate is not None and candidate.language:
            lyric_language = candidate.language
            break
    asr_base = asr_language.split("-")[0] if asr_language else None
    lyric_base = lyric_language.split("-")[0] if lyric_language else None
    if asr_base and lyric_base and asr_base != lyric_base:
        warnings.append("lyrics_language_mismatch")
    return asr_language or lyric_language


def _asr_quality(asr: AsrTranscript | None) -> str:
    if asr is None or asr.empty:
        return "empty"
    bad = False
    if asr.avg_logprob is not None and asr.avg_logprob < _LOW_QUALITY_LOGPROB:
        bad = True
    if asr.no_speech_prob is not None and asr.no_speech_prob >= _HIGH_NO_SPEECH:
        bad = True
    probability = asr.mean_word_probability
    if probability is not None and probability < _LOW_WORD_PROBABILITY:
        bad = True
    return "bad" if bad else "ok"


def _asr_confidence(asr: AsrTranscript, quality: str) -> float:
    if quality == "bad":
        return 0.3
    confidence = 0.55
    if asr.avg_logprob is not None:
        confidence += _clamp((asr.avg_logprob + 1.0) / 2.0, 0.0, 0.15)
    if asr.no_speech_prob is not None:
        confidence -= _clamp(asr.no_speech_prob, 0.0, 0.2)
    return _clamp(confidence)


def _asr_document(asr: AsrTranscript) -> LrcDocument:
    lines: list[LrcLine] = []
    for segment in asr.segments:
        text = normalize_lyrics_text(segment.text)
        if not text:
            continue
        lines.append(
            LrcLine(
                timestamp_ms=max(0, round(segment.start * 1000)),
                text=text,
            )
        )
    return LrcDocument(lines=tuple(lines))


def _asr_elrc(asr: AsrTranscript) -> str | None:
    segments = tuple(segment for segment in asr.segments if segment.words)
    if not segments:
        return None
    text = render_elrc(segments)
    return text or None


def _scale_document(document: LrcDocument, factor: float) -> LrcDocument:
    lines = tuple(
        LrcLine(
            timestamp_ms=max(0, round(line.timestamp_ms * factor)),
            text=line.text,
        )
        for line in document.lines
    )
    return LrcDocument(lines=lines, metadata=document.metadata)


def _tokenize(text: str) -> list[str]:
    return [match.group(0).casefold() for match in _TOKEN_RE.finditer(text)]


def _align_plain_to_asr(
    plain: str,
    asr: AsrTranscript,
    duration: float | None,
) -> _Aligned | None:
    asr_words = [word for word in asr.words() if word.word.strip()]
    if not asr_words:
        return None
    lyric_lines = [line for line in plain.split("\n") if line.strip()]
    if not lyric_lines:
        return None

    lyric_tokens: list[tuple[int, str, str]] = []
    for line_index, line in enumerate(lyric_lines):
        for match in _TOKEN_RE.finditer(line):
            lyric_tokens.append((line_index, match.group(0), match.group(0).casefold()))
    if not lyric_tokens:
        return None

    asr_tokens = [word.word.strip().casefold() for word in asr_words]
    lyric_tokens_text = [token[2] for token in lyric_tokens]
    matcher = SequenceMatcher(None, asr_tokens, lyric_tokens_text, autojunk=False)
    anchors: dict[int, int] = {}
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            anchors[block.b + offset] = block.a + offset

    start_time, end_time = _asr_window(asr, asr_words, duration)
    times: list[tuple[float, float]] = []
    anchor_indexes = sorted(anchors)
    if anchor_indexes:
        for index in range(len(lyric_tokens)):
            if index in anchors:
                word = asr_words[anchors[index]]
                times.append((word.start, word.end))
                continue
            previous = next(
                (candidate for candidate in reversed(anchor_indexes) if candidate < index),
                None,
            )
            following = next(
                (candidate for candidate in anchor_indexes if candidate > index),
                None,
            )
            if previous is None and following is None:
                times.append((start_time, start_time))
            elif previous is None:
                if following is None:
                    continue
                anchor_word = asr_words[anchors[following]]
                span = following
                fraction = index / max(1, span)
                moment = start_time + (anchor_word.start - start_time) * fraction
                times.append((moment, moment))
            elif following is None:
                anchor_word = asr_words[anchors[previous]]
                span = max(1, len(lyric_tokens) - 1 - previous)
                fraction = (index - previous) / span
                end = max(anchor_word.end, end_time)
                moment = anchor_word.end + (end - anchor_word.end) * fraction
                times.append((moment, moment))
            else:
                left_word = asr_words[anchors[previous]]
                right_word = asr_words[anchors[following]]
                span = max(1, following - previous)
                fraction = (index - previous) / span
                moment = left_word.end + (right_word.start - left_word.end) * fraction
                times.append((moment, moment))
    else:
        span = max(1, len(lyric_tokens))
        for index in range(len(lyric_tokens)):
            moment = start_time + (end_time - start_time) * index / span
            times.append((moment, moment))

    # Normalize times to be non-decreasing; DP alignment can jump backwards
    # on repeated lines.
    cursor = start_time
    cleaned: list[tuple[float, float]] = []
    for start, end in times:
        start = max(start, cursor)
        end = max(end, start)
        cleaned.append((start, end))
        cursor = start

    by_line: dict[int, list[tuple[str, float, float]]] = {
        index: [] for index in range(len(lyric_lines))
    }
    for (line_index, text, _), (start, end) in zip(lyric_tokens, cleaned, strict=True):
        by_line[line_index].append((text, start, end))

    lines: list[LrcLine] = []
    segments: list[AsrSegment] = []
    for line_index, line in enumerate(lyric_lines):
        tokens = by_line[line_index]
        if not tokens:
            continue
        line_start = tokens[0][1]
        line_end = max(tokens[-1][2], line_start)
        lines.append(LrcLine(timestamp_ms=max(0, round(line_start * 1000)), text=line.strip()))
        segments.append(
            AsrSegment(
                start=line_start,
                end=line_end,
                text=line.strip(),
                words=[
                    AsrWord(word=text, start=start, end=end)
                    for text, start, end in tokens
                    if text.strip()
                ],
            )
        )
    if not lines:
        return None
    coverage = len(anchors) / max(1, len(lyric_tokens))
    return _Aligned(lines=tuple(lines), segments=tuple(segments), coverage=coverage)


def _asr_window(
    asr: AsrTranscript,
    words: Sequence[AsrWord],
    duration: float | None,
) -> tuple[float, float]:
    start = min((word.start for word in words), default=0.0)
    end = max((word.end for word in words), default=0.0)
    if end <= start:
        end = duration or asr.duration_seconds or start + 1.0
    return start, max(end, start)


def _proportional_document(
    plain: str,
    duration: float | None,
    asr: AsrTranscript | None,
) -> LrcDocument:
    lines = [line.strip() for line in plain.split("\n") if line.strip()]
    if not lines:
        return LrcDocument()
    if asr is not None and asr.segments:
        start = min(segment.start for segment in asr.segments)
        end = max(segment.end for segment in asr.segments)
        total = max(0.0, end - start)
        if total <= 0 and duration:
            total = duration
    else:
        start = 0.0
        total = duration or (asr.duration_seconds if asr is not None else 0.0)
    if total <= 0:
        total = max(float(len(lines)), 1.0)
    weights = [max(1, len(line)) for line in lines]
    weight_total = sum(weights)
    cumulative = 0
    result: list[LrcLine] = []
    for line, weight in zip(lines, weights, strict=True):
        timestamp = round((start + total * cumulative / weight_total) * 1000)
        result.append(LrcLine(timestamp_ms=max(0, timestamp), text=line))
        cumulative += weight
    return LrcDocument(lines=tuple(result))


def _limited(text: str, warnings: list[str]) -> str | None:
    if lyrics_size_bytes(text) > MAX_LYRICS_BYTES:
        warnings.append("lyrics_too_large")
        return None
    return text or None


def _check_size(
    plain: str | None,
    lrc: str | None,
    elrc: str | None,
    warnings: list[str],
) -> None:
    if plain is not None and lyrics_size_bytes(plain) > MAX_LYRICS_BYTES:
        warnings.append("lyrics_too_large")
    if lrc is not None and lyrics_size_bytes(lrc) > MAX_LYRICS_BYTES:
        warnings.append("lyrics_too_large")
    if elrc is not None and lyrics_size_bytes(elrc) > MAX_LYRICS_BYTES:
        warnings.append("lyrics_too_large")


def _similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left.casefold().strip(), right.casefold().strip()).ratio()


def _clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    if not math.isfinite(value):
        return minimum
    return max(minimum, min(maximum, value))


def _derivative_parameters(
    claims: Sequence[Claim] | Sequence[str] | Mapping[str, Any] | None,
) -> tuple[tuple[str, ...], float | None]:
    transformations: list[str] = []
    speed_factor: float | None = None

    def add_transformation(value: object) -> None:
        if isinstance(value, str):
            text = value.strip().casefold().replace("-", "_").replace(" ", "_")
            if text and text not in transformations:
                transformations.append(text)
        elif isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                add_transformation(item)

    def add_speed(value: object) -> None:
        nonlocal speed_factor
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return
        try:
            number = float(value)
        except (TypeError, ValueError):
            return
        if number > 0:
            speed_factor = number

    def consume(item: object) -> None:
        if isinstance(item, str):
            add_transformation(item)
            return
        if isinstance(item, Claim):
            status = getattr(item, "status", ClaimStatus.PROPOSED)
            if status not in _ACTIVE_STATUSES:
                return
            if item.subject not in {Subject.DERIVATIVE, Subject.ITEM}:
                return
            if item.field in _TRANSFORM_FIELDS:
                add_transformation(item.value)
            elif item.field in _SPEED_FIELDS:
                add_speed(item.value)
            return
        if isinstance(item, Mapping):
            for key, value in item.items():
                if key in _TRANSFORM_FIELDS:
                    add_transformation(value)
                elif key in _SPEED_FIELDS:
                    add_speed(value)

    if isinstance(claims, Mapping):
        consume(claims)
    elif claims is not None:
        for claim in claims:
            consume(claim)
    return tuple(transformations), speed_factor


# Imported late to keep the candidate DTO optional for callers that construct
# plain mappings in tests.
__all__ = ["LyricsMerger"]
