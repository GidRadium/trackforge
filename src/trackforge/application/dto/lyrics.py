"""Lyrics, LRC/ELRC, and ASR transcript contracts.

The LRC document keeps timestamps in integer milliseconds so parsing and
rendering never accumulate floating-point error.  ``asr.transcript.v1`` is the
on-disk JSON schema emitted by the Whisper observer and consumed by the lyrics
merger.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model._validation import require_text

MAX_LYRICS_BYTES = 256 * 1024
ISO_639_RE = re.compile(r"^[a-z]{2,3}(?:[-_][a-z]{2,4})?$")


def normalize_lyrics_text(text: str) -> str:
    """Normalize line endings/whitespace without rewriting the words."""

    if not isinstance(text, str):
        raise DomainValidationError("lyrics text must be a string")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in normalized.split("\n")]
    return "\n".join(lines).strip()


def lyrics_size_bytes(text: str | None) -> int:
    return len(text.encode("utf-8")) if text else 0


def valid_language(value: str | None) -> str | None:
    """Return a lower-case ISO 639 code, or ``None`` when it is not evidence."""

    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    return text if ISO_639_RE.fullmatch(text) else None


@dataclass(frozen=True, slots=True)
class LrcLine:
    """One synchronized lyric line."""

    timestamp_ms: int
    text: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.timestamp_ms, bool) or not isinstance(self.timestamp_ms, int):
            raise DomainValidationError("timestamp_ms must be an integer")
        if self.timestamp_ms < 0:
            raise DomainValidationError("timestamp_ms must not be negative")
        if not isinstance(self.text, str):
            raise DomainValidationError("LRC line text must be a string")
        object.__setattr__(self, "text", self.text.strip())


@dataclass(frozen=True, slots=True)
class LrcMetadata:
    """Portable LRC tag header; unknown tags are intentionally dropped."""

    title: str | None = None
    artist: str | None = None
    album: str | None = None
    by: str | None = None
    offset_ms: int | None = None

    def __post_init__(self) -> None:
        for name in ("title", "artist", "album", "by"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise DomainValidationError(f"LRC {name} must be a string or None")
            if isinstance(value, str):
                object.__setattr__(self, name, value.strip() or None)
        if self.offset_ms is not None:
            if isinstance(self.offset_ms, bool) or not isinstance(self.offset_ms, int):
                raise DomainValidationError("LRC offset_ms must be an integer or None")

    def is_empty(self) -> bool:
        return (
            self.title is None
            and self.artist is None
            and self.album is None
            and self.by is None
            and self.offset_ms is None
        )


@dataclass(frozen=True, slots=True)
class LrcDocument:
    """A validated, monotonically ordered LRC document."""

    lines: tuple[LrcLine, ...] = ()
    metadata: LrcMetadata = field(default_factory=LrcMetadata)

    def __post_init__(self) -> None:
        lines = tuple(self.lines)
        for index, line in enumerate(lines):
            if not isinstance(line, LrcLine):
                raise DomainValidationError(f"lines[{index}] must be an LrcLine")
        if not isinstance(self.metadata, LrcMetadata):
            raise DomainValidationError("metadata must be an LrcMetadata")
        previous = -1
        for line in lines:
            if line.timestamp_ms < previous:
                raise DomainValidationError(
                    "LRC timestamps must be monotonic",
                    context={"timestamp_ms": line.timestamp_ms},
                )
            previous = line.timestamp_ms
        object.__setattr__(self, "lines", lines)

    @classmethod
    def parse(cls, text: str) -> LrcDocument:
        from trackforge.application.services.lrc import parse_lrc

        return parse_lrc(text)

    def render(self) -> str:
        from trackforge.application.services.lrc import render_lrc

        return render_lrc(self)

    def plain_text(self) -> str:
        return normalize_lyrics_text("\n".join(line.text for line in self.lines))

    @property
    def has_timing(self) -> bool:
        return bool(self.lines)


class AsrWord(BaseModel):
    """One word with an absolute timestamp inside the audio."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    word: str = Field(min_length=1)
    start: float = Field(ge=0.0)
    end: float = Field(ge=0.0)
    probability: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _ordered(self) -> AsrWord:
        if self.end < self.start:
            raise ValueError("word end must not precede start")
        return self


class AsrSegment(BaseModel):
    """One decoded segment and its optional word-level alignment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start: float = Field(ge=0.0)
    end: float = Field(ge=0.0)
    text: str
    words: list[AsrWord] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ordered(self) -> AsrSegment:
        if self.end < self.start:
            raise ValueError("segment end must not precede start")
        return self


class AsrModelInfo(BaseModel):
    """Identity of the ASR runtime/model used for one transcript."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    version: str = "unknown"
    compute_type: str = Field(default="int8", min_length=1)
    threads: int = Field(default=8, ge=1)


class AsrTranscript(BaseModel):
    """Schema for the ``asr.transcript.v1`` artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    language: str | None = None
    segments: list[AsrSegment] = Field(default_factory=list)
    avg_logprob: float | None = None
    no_speech_prob: float | None = Field(default=None, ge=0.0, le=1.0)
    model: AsrModelInfo
    duration_seconds: float = Field(default=0.0, ge=0.0)

    @field_validator("language")
    @classmethod
    def _language(cls, value: object) -> str | None:
        if value is None:
            return None
        text = valid_language(str(value))
        if text is None:
            raise ValueError("language must be an ISO 639 code or null")
        return text

    @model_validator(mode="after")
    def _monotonic(self) -> AsrTranscript:
        previous = -1.0
        for segment in self.segments:
            if segment.start < previous:
                raise ValueError("segments must be ordered by start time")
            previous = segment.start
        return self

    def plain_text(self) -> str:
        return normalize_lyrics_text("\n".join(segment.text for segment in self.segments))

    @property
    def empty(self) -> bool:
        return not self.segments or not self.plain_text().strip()

    def words(self) -> list[AsrWord]:
        return [word for segment in self.segments for word in segment.words]

    @property
    def mean_word_probability(self) -> float | None:
        probabilities = [word.probability for word in self.words() if word.probability is not None]
        if not probabilities:
            return None
        return sum(probabilities) / len(probabilities)


@dataclass(frozen=True, slots=True)
class LyricsResult:
    """Merged plain/LRC/ELRC text plus its provenance and warnings."""

    plain: str | None = None
    lrc: str | None = None
    elrc: str | None = None
    source: str = "unknown"
    confidence: float = 0.0
    warnings: tuple[str, ...] = ()
    language: str | None = None
    timing_source: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", require_text(self.source, "source"))
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise DomainValidationError("lyrics confidence must be between 0 and 1")
        object.__setattr__(self, "warnings", tuple(dict.fromkeys(self.warnings)))
        object.__setattr__(self, "language", valid_language(self.language))
        if self.plain is not None and not isinstance(self.plain, str):
            raise DomainValidationError("plain lyrics must be a string or None")
        for name in ("lrc", "elrc"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise DomainValidationError(f"{name} must be a string or None")
        if self.timing_source is not None:
            object.__setattr__(
                self,
                "timing_source",
                require_text(self.timing_source, "timing_source"),
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "plain": self.plain,
            "lrc": self.lrc,
            "elrc": self.elrc,
            "source": self.source,
            "confidence": self.confidence,
            "warnings": list(self.warnings),
            "language": self.language,
            "timing_source": self.timing_source,
        }


__all__ = [
    "ISO_639_RE",
    "MAX_LYRICS_BYTES",
    "AsrModelInfo",
    "AsrSegment",
    "AsrTranscript",
    "AsrWord",
    "LrcDocument",
    "LrcLine",
    "LrcMetadata",
    "LyricsResult",
    "lyrics_size_bytes",
    "normalize_lyrics_text",
    "valid_language",
]
