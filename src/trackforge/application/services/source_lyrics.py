"""Extract plain lyrics from an explicit ``Lyrics:`` description section.

Descriptions are untrusted source text.  This parser only accepts an explicit
section marker, stops at common footer markers, and refuses timed/LRC blocks
so a plain-lyrics tag can never be populated with timestamps.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from trackforge.domain.policies.normalization import normalize_text

MAX_DESCRIPTION_LYRICS_BYTES = 256 * 1024
MIN_DESCRIPTION_LYRICS_CHARS = 80

_LYRICS_HEADER_RE = re.compile(
    r"^\s*lyrics?\s*(?::\s*(?P<inline>.*))?\s*$",
    re.IGNORECASE,
)
_FOOTER_RE = re.compile(
    r"^\s*(?:"
    r"original\s+(?:title|song|track|artist|art)\b|"
    r"follow\b|"
    r"more\b|"
    r"vk\s*:|"
    r"my\s+soundcloud\b|"
    r"soundcloud\s*:|"
    r"all\s+arts?\b|"
    r"business\s*(?:contact|inquiries?)?\b|"
    r"nightcore\s+mixes\b|"
    r"contact\b|"
    r"tags?\s*:"
    r")",
    re.IGNORECASE,
)
_LRC_TIMESTAMP_RE = re.compile(r"\[\s*\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?\s*\]")
_URL_ONLY_RE = re.compile(r"^(?:https?://|www\.)\S+$", re.IGNORECASE)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class DescriptionLyrics:
    """One explicit description lyrics section.

    ``text`` is ``None`` when the section was found but deliberately skipped
    (timed, too short, URL-only, ...).  ``warnings`` explains the skip so the
    command line and sidecar can expose the same degradation.
    """

    text: str | None
    section: str
    confidence: float
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        text = self.text.strip() if isinstance(self.text, str) else None
        object.__setattr__(self, "text", text or None)
        object.__setattr__(self, "section", self.section.strip() or "lyrics")
        object.__setattr__(
            self,
            "confidence",
            max(0.0, min(1.0, float(self.confidence))),
        )
        object.__setattr__(self, "warnings", tuple(dict.fromkeys(self.warnings)))


def extract_description_lyrics(description: str | None) -> DescriptionLyrics | None:
    """Return the explicit description lyrics section, if one exists."""

    if not description or not description.strip():
        return None
    lines = description.splitlines()
    start: int | None = None
    collected: list[str] = []
    for index, raw_line in enumerate(lines):
        match = _LYRICS_HEADER_RE.match(raw_line)
        if match is None:
            continue
        start = index
        inline = (match.group("inline") or "").strip()
        if inline:
            collected.append(inline)
        break
    if start is None:
        return None

    for raw_line in lines[start + 1 :]:
        line = raw_line.rstrip()
        if _FOOTER_RE.match(line):
            break
        collected.append(line)
    text = "\n".join(collected).strip()
    text = normalize_text(text) if "\n" not in text else _normalize_block(text)
    if not text:
        return DescriptionLyrics(
            text=None,
            section="lyrics",
            confidence=0.0,
            warnings=("description_lyrics_empty",),
        )
    if _LRC_TIMESTAMP_RE.search(text):
        return DescriptionLyrics(
            text=None,
            section="lyrics",
            confidence=0.0,
            warnings=("description_lyrics_synced_skipped",),
        )
    non_empty = [line.strip() for line in text.splitlines() if line.strip()]
    if not non_empty or all(_URL_ONLY_RE.fullmatch(line) for line in non_empty):
        return DescriptionLyrics(
            text=None,
            section="lyrics",
            confidence=0.0,
            warnings=("description_lyrics_url_only",),
        )
    without_urls = _URL_RE.sub("", text)
    if len(normalize_text(without_urls)) < MIN_DESCRIPTION_LYRICS_CHARS:
        return DescriptionLyrics(
            text=None,
            section="lyrics",
            confidence=0.0,
            warnings=("description_lyrics_too_short",),
        )
    if len(text.encode("utf-8")) > MAX_DESCRIPTION_LYRICS_BYTES:
        text = _truncate_bytes(text, MAX_DESCRIPTION_LYRICS_BYTES)
        return DescriptionLyrics(
            text=text,
            section="lyrics",
            confidence=0.4,
            warnings=("description_lyrics_truncated",),
        )
    return DescriptionLyrics(
        text=text,
        section="lyrics",
        confidence=0.55,
    )


def _normalize_block(value: str) -> str:
    lines = [line.rstrip() for line in value.splitlines()]
    result: list[str] = []
    blank = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if not blank and result:
                result.append("")
            blank = True
            continue
        blank = False
        result.append(stripped)
    return "\n".join(result).strip()


def _truncate_bytes(value: str, limit: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    return encoded[:limit].decode("utf-8", errors="ignore").rstrip()


__all__ = [
    "MAX_DESCRIPTION_LYRICS_BYTES",
    "MIN_DESCRIPTION_LYRICS_CHARS",
    "DescriptionLyrics",
    "extract_description_lyrics",
]
