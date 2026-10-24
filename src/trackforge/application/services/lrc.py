"""Parse, validate, and render LRC; render enhanced LRC (ELRC).

Only ``[mm:ss.xx]`` timestamps (two or three fractional digits) are accepted.
The parser keeps every emitted line in non-decreasing timestamp order and
rejects malformed input instead of silently shifting lyrics.  ELRC uses the
common inline ``<mm:ss.xx>`` word form.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from trackforge.application.dto.lyrics import (
    AsrSegment,
    LrcDocument,
    LrcLine,
    LrcMetadata,
    normalize_lyrics_text,
)
from trackforge.domain.errors import DomainValidationError

_TIMESTAMP_RE = re.compile(r"\[(\d{2,}):([0-5]\d)[.:](\d{2,3})\]")
_METADATA_RE = re.compile(r"^\[([A-Za-z][A-Za-z0-9#_-]*):(.*)\]$")
_KNOWN_METADATA = {
    "ti": "title",
    "ar": "artist",
    "al": "album",
    "by": "by",
    "offset": "offset_ms",
}


class LrcParseError(DomainValidationError):
    """An invalid LRC document with the source line number."""

    def __init__(self, message: str, *, line_number: int | None = None) -> None:
        context = {"code": "invalid_lrc"}
        if line_number is not None:
            context["line"] = str(line_number)
        super().__init__(message, context=context)
        self.line_number = line_number


def format_lrc_timestamp(timestamp_ms: int) -> str:
    """Format non-negative milliseconds as ``[mm:ss.xx]``."""

    if isinstance(timestamp_ms, bool) or not isinstance(timestamp_ms, int):
        raise DomainValidationError("timestamp_ms must be an integer")
    if timestamp_ms < 0:
        raise DomainValidationError("timestamp_ms must not be negative")
    if timestamp_ms % 10:
        # Keep millisecond precision when the source used three digits.
        minutes, remainder = divmod(timestamp_ms, 60_000)
        seconds, milliseconds = divmod(remainder, 1000)
        return f"[{minutes:02d}:{seconds:02d}.{milliseconds:03d}]"
    centiseconds = timestamp_ms // 10
    minutes, remainder = divmod(centiseconds, 6000)
    seconds, fraction = divmod(remainder, 100)
    return f"[{minutes:02d}:{seconds:02d}.{fraction:02d}]"


def format_elrc_timestamp(timestamp_ms: int) -> str:
    return f"<{format_lrc_timestamp(timestamp_ms)[1:-1]}>"


def _fraction_to_ms(fraction: str) -> int:
    if len(fraction) == 2:
        return int(fraction) * 10
    return int(fraction)


def parse_lrc(text: str) -> LrcDocument:
    """Parse LRC text; raise :class:`LrcParseError` on invalid timing/syntax."""

    if not isinstance(text, str):
        raise LrcParseError("LRC input must be text")
    document = text.lstrip("\ufeff")
    lines: list[LrcLine] = []
    metadata: dict[str, object] = {}
    previous = -1

    for number, raw_line in enumerate(document.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue

        metadata_match = _METADATA_RE.fullmatch(line)
        if metadata_match is not None:
            key = metadata_match.group(1).strip().lower()
            value = metadata_match.group(2).strip()
            target = _KNOWN_METADATA.get(key)
            if target is None:
                # Unknown LRC headers are legal extensions; ignore them.
                continue
            if target == "offset_ms":
                try:
                    parsed: object = int(value)
                except ValueError as exc:
                    raise LrcParseError(
                        "LRC offset must be an integer number of milliseconds",
                        line_number=number,
                    ) from exc
            else:
                parsed = value
            metadata[target] = parsed
            continue

        position = 0
        stamps: list[int] = []
        while True:
            timestamp = _TIMESTAMP_RE.match(line, position)
            if timestamp is None:
                break
            minutes = int(timestamp.group(1))
            seconds = int(timestamp.group(2))
            milliseconds = _fraction_to_ms(timestamp.group(3))
            stamps.append((minutes * 60 + seconds) * 1000 + milliseconds)
            position = timestamp.end()
        if not stamps:
            raise LrcParseError(
                f"line {number} does not start with a valid LRC timestamp",
                line_number=number,
            )
        lyric_text = line[position:].strip()
        for timestamp_ms in stamps:
            if timestamp_ms < previous:
                raise LrcParseError(
                    "LRC timestamps must be monotonic",
                    line_number=number,
                )
            previous = timestamp_ms
            lines.append(LrcLine(timestamp_ms=timestamp_ms, text=lyric_text))

    try:
        return LrcDocument(
            lines=tuple(lines),
            metadata=LrcMetadata(**metadata),  # type: ignore[arg-type]
        )
    except DomainValidationError as exc:
        raise LrcParseError(exc.message) from exc


def try_parse_lrc(text: str) -> tuple[LrcDocument | None, tuple[str, ...]]:
    """Return a document or warnings; never raise for malformed candidate text."""

    try:
        document = parse_lrc(text)
    except LrcParseError:
        return None, ("invalid_lrc",)
    if not document.lines:
        return None, ("invalid_lrc",)
    return document, ()


def is_valid_lrc(text: str | None) -> bool:
    if not text or not text.strip():
        return False
    document, _ = try_parse_lrc(text)
    return document is not None


def _render_metadata(metadata: LrcMetadata) -> list[str]:
    values: list[str] = []
    for key, field in (
        ("ti", metadata.title),
        ("ar", metadata.artist),
        ("al", metadata.album),
        ("by", metadata.by),
    ):
        if field:
            values.append(f"[{key}:{_single_line(field)}]")
    if metadata.offset_ms is not None:
        values.append(f"[offset:{metadata.offset_ms}]")
    return values


def _single_line(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ").strip()


def render_lrc(document: LrcDocument) -> str:
    """Render a validated document as UTF-8 LRC text (trailing newline)."""

    if not isinstance(document, LrcDocument):
        raise DomainValidationError("document must be an LrcDocument")
    output = _render_metadata(document.metadata)
    output.extend(
        f"{format_lrc_timestamp(line.timestamp_ms)}{line.text}" for line in document.lines
    )
    if not output:
        return ""
    return "\n".join(output) + "\n"


def render_elrc(
    segments: Sequence[AsrSegment],
    *,
    metadata: LrcMetadata | None = None,
) -> str:
    """Render word-level enhanced LRC from ASR segments.

    Format: ``[mm:ss.xx]<mm:ss.xx>word <mm:ss.xx>word``.  Segments without
    words are skipped; words without timestamps cannot be represented and are
    therefore omitted.
    """

    output = _render_metadata(metadata) if metadata is not None else []
    for segment in segments:
        words = segment.words
        if not words:
            continue
        parts = [format_lrc_timestamp(round(segment.start * 1000))]
        first = True
        for word in words:
            text = _escape_word(word.word.strip())
            if not text:
                continue
            stamp = format_elrc_timestamp(round(word.start * 1000))
            parts.append(f"{stamp}{text}" if first else f" {stamp}{text}")
            first = False
        if first:
            continue
        output.append("".join(parts))
    if not output:
        return ""
    return "\n".join(output) + "\n"


def _escape_word(value: str) -> str:
    # ``<``/``>`` delimit inline timestamps, so escape them without changing
    # the audible word content in a surprising way.
    return value.replace("<", "‹").replace(">", "›")


def lrc_plain_text(document: LrcDocument) -> str:
    """Return the text of a document without timestamps."""

    return normalize_lyrics_text("\n".join(line.text for line in document.lines))


__all__ = [
    "LrcParseError",
    "format_elrc_timestamp",
    "format_lrc_timestamp",
    "is_valid_lrc",
    "lrc_plain_text",
    "parse_lrc",
    "render_elrc",
    "render_lrc",
    "try_parse_lrc",
]
