"""Normalize noisy source titles into a clean title/artist/qualifier.

The source document is evidence, not truth.  This module never mutates the
snapshot: it returns a small, deterministic interpretation that the
``source_metadata`` interpreter can turn into low-risk claims.  In particular
it removes YouTube-style counters/noise, recognizes derivative channel
prefixes, and prefers an explicit ``Original title:`` line from anywhere in
the description over the noisy upload title.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from trackforge.domain.model.identity import Transformation
from trackforge.domain.policies.normalization import (
    comparison_key,
    detect_transformations,
    display_qualifier,
    normalize_artist_name,
    normalize_text,
    rank_transformations,
    strip_qualifier,
    strip_topic_suffix,
)

_ORIGINAL_LINE_RE = re.compile(
    r"^\s*original\s+(?:song\s+)?(?:title|song|track)\s*[:\-–—]\s*(?P<value>.+?)\s*$",
    re.IGNORECASE,
)
_ORIGINAL_ARTIST_LINE_RE = re.compile(
    r"^\s*original\s+artist(?:\s*\(s\))?\s*[:\-–—]\s*(?P<value>.+?)\s*$",
    re.IGNORECASE,
)
_ARTIST_LABEL_RE = re.compile(
    r"^\s*(?:original\s+artist(?:\s*\(s\))?|artist(?:\s*\(s\))?|"
    r"performed\s+by|original\s+by|by)\s*[:\-–—]\s*(?P<value>.+?)\s*$",
    re.IGNORECASE,
)
_ARTIST_HEADER_RE = re.compile(
    r"^\s*(?:please\s+)?support\s+the\s+original\s+artist(?:\s*\(s\))?"
    r"\s*[:\-–—]?\s*(?P<value>.*?)\s*$",
    re.IGNORECASE,
)
_ORIGINAL_ARTISTS_RE = re.compile(
    r"^\s*original\s+artists?\s*[:\-–—]\s*(?P<value>.+?)\s*$",
    re.IGNORECASE,
)
_TRAILING_ARTIST_RE = re.compile(r"^(?P<title>.+?)\s*[\[(](?P<artist>[^\])]{2,80})[\])]\s*$")
_TRAILING_ARTIST_NOISE = frozenset(
    {
        "acoustic",
        "audio",
        "cover",
        "edit",
        "explicit",
        "extended",
        "feat",
        "ft",
        "hd",
        "hq",
        "instrumental",
        "live",
        "lyric",
        "lyrics",
        "nightcore",
        "official",
        "remix",
        "rock cover",
        "slowed",
        "sped up",
        "version",
        "video",
        "visualizer",
    }
)
_ATTRIBUTION_LINE_RE = re.compile(
    r"^\s*(?P<title>.+?)\s+by\s+(?P<artist>.+?)"
    r"(?:\s+from\s+(?P<album>.+?))?\.?\s*$",
    re.IGNORECASE,
)
_COUNTER_RE = re.compile(r"^\s*(?:\{\s*\d{1,4}\s*\}|\[\s*\d{1,4}\s*\]|\(\s*\d{1,4}\s*\))\s*")
_PAREN_DERIVATIVE_RE = re.compile(
    r"^\s*(?P<label>nightcore|sped\s*up|slowed(?:\s*\+\s*reverb)?|remix)\s*"
    r"\((?P<artist>[^)]+)\)\s*(?:[-–—:|]\s*)?(?P<rest>.*)$",
    re.IGNORECASE,
)
_DERIVATIVE_PREFIX_RE = re.compile(
    r"^\s*(?P<label>nightcore|sped\s*up|slowed(?:\s*\+\s*reverb)?|remix)\s*"
    r"(?:[-–—:|]\s*)?",
    re.IGNORECASE,
)
_DASH_SPLIT_RE = re.compile(r"\s+[-–—]\s+")
_TRAILING_QUALIFIER_RE = re.compile(
    r"\s*[\(\[]\s*(?:nightcore|sped\s*up|slowed(?:\s*\+\s*reverb)?|remix)"
    r"\s*[\)\]]\s*$",
    re.IGNORECASE,
)
_TRAILING_BARE_QUALIFIER_RE = re.compile(
    r"(?<=.)\s*(?:[-–—|]\s*)?(?:nightcore|sped\s*up|slowed(?:\s*\+\s*reverb)?|remix)"
    r"\s*$",
    re.IGNORECASE,
)
_NOISE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\s*[\(\[]\s*(?:with\s+)?lyrics?\s*[\)\]]", re.IGNORECASE),
    re.compile(
        r"\s*[\(\[]\s*official\s+(?:music\s+)?(?:video|audio|visuali[sz]er)\s*[\)\]]",
        re.IGNORECASE,
    ),
    re.compile(
        r"\s*[\(\[]\s*(?:lyric|music)\s+video\s*[\)\]]",
        re.IGNORECASE,
    ),
    re.compile(
        r"\s*[\(\[]\s*(?:hd|hq|4k|8k|uhd|mv|audio\s+only)\s*[\)\]]",
        re.IGNORECASE,
    ),
    re.compile(r"\s*[\[(]\s*(?:official)\s*[\])]", re.IGNORECASE),
    re.compile(r"\s*[\[(]\s*(?:color\s+coded|coloured)\s*[\])]", re.IGNORECASE),
)
_TRAILING_BRACKET_NOISE_RE = re.compile(
    r"\s+(?:official\s+(?:video|audio)|lyrics?|hd|hq|4k|8k)\s*$",
    re.IGNORECASE,
)
_DERIVATIVE_LABELS: frozenset[Transformation] = frozenset(
    {
        Transformation.NIGHTCORE,
        Transformation.SPED_UP,
        Transformation.SLOWED,
        Transformation.REMIX,
    }
)
_MIX_TITLE_RE = re.compile(
    r"\b(?:rock|metal|pop|edm|dance|nightcore|anime|music|song)?\s*"
    r"(?:mix|mixtape|megamix|compilation|medley|playlist)\b",
    re.IGNORECASE,
)
_QUALIFIER_WORDS = frozenset(
    {
        "nightcore",
        "remix",
        "sped",
        "slowed",
        "reverb",
        "lyrics",
        "lyric",
        "official",
        "video",
        "audio",
    }
)


@dataclass(frozen=True, slots=True)
class NormalizedSourceIdentity:
    """A conservative interpretation of one uploader-supplied title."""

    title: str | None
    artist: str | None
    qualifier: str | None
    source_title: str
    confidence: float
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("title", "artist", "qualifier"):
            value = getattr(self, name)
            if value is not None:
                text = value.strip()
                object.__setattr__(self, name, text or None)
        object.__setattr__(self, "source_title", str(self.source_title))
        value = float(self.confidence)
        object.__setattr__(self, "confidence", max(0.0, min(1.0, value)))
        object.__setattr__(self, "warnings", tuple(dict.fromkeys(self.warnings)))


def looks_like_mix(
    title: str | None,
    duration_seconds: float | None = None,
    *,
    uploader: str | None = None,
    description: str | None = None,
) -> bool:
    """Return whether source text describes a long mix/compilation, not a song.

    A short track called ``Rock Mix`` may be a normal remix; only long audio
    (10+ minutes) is treated as a compilation.  Explicit ``megamix`` /
    ``compilation`` titles are accepted without a duration when the wording is
    unambiguous.
    """

    texts = " ".join(
        part for part in (title, uploader, description) if isinstance(part, str) and part.strip()
    )
    if not texts:
        return False
    match = _MIX_TITLE_RE.search(texts)
    if match is None:
        return False
    word = match.group(0).casefold()
    if "megamix" in word or "compilation" in word:
        return True
    try:
        duration = float(duration_seconds) if duration_seconds is not None else 0.0
    except (TypeError, ValueError):
        duration = 0.0
    return duration >= 600.0


def normalize_source_identity(
    title: str | None,
    uploader: str | None,
    description: str | None,
) -> NormalizedSourceIdentity:
    """Return a clean title/artist/qualifier and provenance warnings."""

    source_title = normalize_text(title or "")
    uploader_text = _clean_channel(uploader)
    warnings: list[str] = []
    if not source_title:
        return NormalizedSourceIdentity(
            title=None,
            artist=None,
            qualifier=None,
            source_title=source_title,
            confidence=0.0,
            warnings=("source_title_missing",),
        )

    title_transformations = detect_transformations(source_title)
    uploader_transformations = detect_transformations(uploader_text or "")
    all_transformations = tuple(dict.fromkeys((*title_transformations, *uploader_transformations)))
    supported = tuple(item for item in all_transformations if item in _DERIVATIVE_LABELS)
    qualifier = display_qualifier(supported[0]) if supported else None

    if _COUNTER_RE.match(source_title):
        warnings.append("source_counter_removed")
    if any(pattern.search(source_title) for pattern in _NOISE_PATTERNS):
        warnings.append("source_title_noise_removed")

    original_title, original_artist, original_qualifier = _original_from_description(description)
    if original_title is not None:
        _append_unique(warnings, "original_title_from_description")
        if original_qualifier is not None and qualifier is None:
            qualifier = original_qualifier
        if original_artist is None:
            original_artist = _description_artist(description, original_title)
        if original_artist is None and uploader_text and not supported:
            original_artist = uploader_text
        confidence = 0.9
        if original_artist is None and supported:
            _append_unique(warnings, "original_artist_unknown")
        return NormalizedSourceIdentity(
            title=original_title,
            artist=original_artist,
            qualifier=qualifier,
            source_title=source_title,
            confidence=confidence,
            warnings=tuple(warnings),
        )

    parsed_title, parsed_artist, parse_confidence = _parse_upload_title(
        source_title,
        uploader_text,
        derivative=bool(supported),
    )
    if parsed_title is None:
        parsed_title = _clean_title(source_title)
        parse_confidence = 0.45
        warnings.append("source_title_unparsed")
    if parsed_artist is None:
        parsed_artist = _description_artist(description, parsed_title or source_title)
        if parsed_artist is not None:
            parse_confidence = max(parse_confidence, 0.75)
    if parsed_artist is None and uploader_text and not supported:
        parsed_artist = uploader_text
        parse_confidence = min(parse_confidence, 0.6)
    if parsed_artist is None and supported:
        _append_unique(warnings, "original_artist_unknown")
    if parsed_artist is not None and uploader_text is not None:
        if comparison_key(parsed_artist) == comparison_key(uploader_text) and not supported:
            parsed_artist = uploader_text
    if parsed_title:
        parsed_title = _clean_title(parsed_title)
    if qualifier and parsed_title:
        # The qualifier is always stored separately from the title.
        parsed_title = strip_qualifier(parsed_title, supported[0])
    if parsed_title:
        parsed_title, trailing_artist = _split_trailing_artist(parsed_title)
        if parsed_artist is None:
            parsed_artist = trailing_artist
    if not parsed_title:
        parsed_title = None
        parse_confidence = 0.0
        warnings.append("source_title_empty")
    return NormalizedSourceIdentity(
        title=parsed_title,
        artist=parsed_artist,
        qualifier=qualifier,
        source_title=source_title,
        confidence=parse_confidence,
        warnings=tuple(warnings),
    )


def _parse_upload_title(
    value: str,
    uploader: str | None,
    *,
    derivative: bool,
) -> tuple[str | None, str | None, float]:
    text = _strip_leading_counters(value)
    text = _strip_noise(text)
    match = _PAREN_DERIVATIVE_RE.match(text)
    if match is not None:
        artist = _clean_artist(match.group("artist"))
        rest = _clean_title(match.group("rest")) or None
        if rest:
            return rest, artist, 0.82
        return None, artist, 0.5

    segments = [segment.strip() for segment in _DASH_SPLIT_RE.split(text) if segment.strip()]
    if len(segments) >= 2:
        raw_first = _strip_noise(_strip_leading_counters(segments[0])).strip(" \t-–—_.")
        first = _clean_artist(segments[0]) or ""
        rest = " - ".join(segments[1:])
        matches_uploader = bool(
            uploader
            and (
                (first and comparison_key(first) == comparison_key(uploader))
                or (raw_first and comparison_key(raw_first) == comparison_key(uploader))
            )
        )
        if matches_uploader and (
            len(segments) >= 3 or bool(detect_transformations(uploader or ""))
        ):
            return _parse_upload_title(rest, uploader, derivative=derivative)
        if raw_first and _looks_like_marker_only(raw_first) and len(segments) >= 3:
            # ``Nightcore - Artist - Title``: the marker is not an artist.
            return (
                _clean_title(" - ".join(segments[2:])) or None,
                _clean_artist(segments[1]),
                0.78,
            )
        prefix = _DERIVATIVE_PREFIX_RE.match(raw_first)
        if prefix is not None and not _strip_channel_marker(raw_first):
            return _clean_title(rest) or None, None, 0.7
        if first and not _looks_like_marker_only(first):
            return _clean_title(rest) or None, first, 0.8 if not derivative else 0.78
        return _clean_title(rest) or None, None, 0.55

    marker = _DERIVATIVE_PREFIX_RE.match(text)
    if marker is not None:
        rest = text[marker.end() :].strip()
        marker_text = _clean_title(rest) or None
        if marker_text:
            return marker_text, None, 0.7
    # ``Artist - Topic`` style channel-only values are not titles.
    cleaned = _clean_title(text)
    return cleaned or None, None, 0.7


def _clean_title(value: str) -> str:
    text = _strip_leading_counters(value)
    text = _strip_noise(text)
    text = _TRAILING_QUALIFIER_RE.sub("", text)
    text = _TRAILING_BARE_QUALIFIER_RE.sub("", text)
    text = _TRAILING_BRACKET_NOISE_RE.sub("", text)
    text = text.strip(" \t-–—_.")
    return normalize_text(text)


def _clean_artist(value: str) -> str | None:
    text = _strip_noise(_strip_leading_counters(value)).strip(" \t-–—_.")
    if not text:
        return None
    match = _PAREN_DERIVATIVE_RE.match(text)
    if match is not None:
        text = match.group("artist")
    prefix = _DERIVATIVE_PREFIX_RE.match(text)
    if prefix is not None:
        candidate = text[prefix.end() :].strip(" \t-–—_.")
        if candidate and not candidate.startswith("("):
            text = candidate
    text = strip_topic_suffix(text)
    artist = normalize_artist_name(text)
    if not artist:
        return None
    if _looks_like_marker_only(artist):
        return None
    return artist.strip(" \t-–—_.") or None


def _clean_channel(value: str | None) -> str | None:
    if value is None:
        return None
    text = strip_topic_suffix(normalize_text(value))
    return text or None


def _strip_leading_counters(value: str) -> str:
    text = value.strip()
    while True:
        match = _COUNTER_RE.match(text)
        if match is None or match.end() == 0:
            return text
        text = text[match.end() :].strip()


def _strip_noise(value: str) -> str:
    text = value
    for pattern in _NOISE_PATTERNS:
        text = pattern.sub(" ", text)
    return normalize_text(text)


def _strip_channel_marker(value: str) -> str:
    return _DERIVATIVE_PREFIX_RE.sub("", value).strip(" \t-–—_.")


def _looks_like_marker_only(value: str) -> bool:
    tokens = {token for token in re.split(r"[\s_+]+", value.casefold()) if token}
    return bool(tokens) and tokens <= _QUALIFIER_WORDS


def _description_artist(description: str | None, title: str | None = None) -> str | None:
    if not description:
        return None
    title_key = comparison_key(title) if title else ""
    lines = description.splitlines()
    for index, raw_line in enumerate(lines[:80]):
        line = raw_line.strip()
        if not line:
            continue
        match = _ORIGINAL_ARTIST_LINE_RE.match(line)
        if match is not None:
            artist = _clean_artist(match.group("value"))
            if artist:
                return artist
        for pattern in (_ARTIST_LABEL_RE, _ORIGINAL_ARTISTS_RE):
            match = pattern.match(line)
            if match is not None:
                artist = _clean_artist(match.group("value"))
                if artist:
                    return artist
        header = _ARTIST_HEADER_RE.match(line)
        if header is not None:
            value = (header.group("value") or "").strip()
            artist = _clean_artist(value) if value else None
            if artist:
                return artist
            artist = _next_artist_line(lines, index + 1)
            if artist:
                return artist
        attribution = _ATTRIBUTION_LINE_RE.match(line)
        if attribution is not None:
            parsed_title = (attribution.group("title") or "").strip(" .")
            if title_key and comparison_key(parsed_title) not in title_key:
                continue
            artist = _clean_artist(attribution.group("artist"))
            if artist and not _looks_like_marker_only(artist):
                return artist
    return None


def _next_artist_line(lines: list[str], start: int) -> str | None:
    for raw_line in lines[start : start + 4]:
        line = raw_line.strip().strip("~*♫-–— ").strip()
        if not line or "http://" in line.casefold() or "https://" in line.casefold():
            continue
        if line.casefold().startswith(("support", "picture", "official", "if you like")):
            continue
        if set(line) <= {"▬", "-", "—", "–", " "}:
            continue
        value = line.partition(":")[0] if ":" in line else line
        artist = _clean_artist(value)
        if artist and not _looks_like_marker_only(artist):
            return artist
    return None


def _split_trailing_artist(title: str) -> tuple[str, str | None]:
    match = _TRAILING_ARTIST_RE.match(title.strip())
    if match is None:
        return title, None
    candidate = _clean_artist(match.group("artist"))
    if candidate is None:
        return title, None
    if candidate.casefold() in _TRAILING_ARTIST_NOISE or candidate.isdigit():
        return title, None
    if any(word in candidate.casefold().split() for word in _TRAILING_ARTIST_NOISE):
        return title, None
    base = _clean_title(match.group("title"))
    if not base:
        return title, None
    return base, candidate


def _original_from_description(
    description: str | None,
) -> tuple[str | None, str | None, str | None]:
    if not description:
        return None, None, None
    for raw_line in description.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _ORIGINAL_LINE_RE.match(line)
        if match is None:
            continue
        value = _strip_noise(match.group("value"))
        qualifier = _description_qualifier(value)
        title_part: str | None = None
        artist_part: str | None = None
        if " - " in value:
            head, _, tail = value.partition(" - ")
            artist_part = _clean_artist(head)
            title_part = _clean_title(tail)
        elif " – " in value:
            head, _, tail = value.partition(" – ")
            artist_part = _clean_artist(head)
            title_part = _clean_title(tail)
        elif " — " in value:
            head, _, tail = value.partition(" — ")
            artist_part = _clean_artist(head)
            title_part = _clean_title(tail)
        else:
            attribution = _ATTRIBUTION_LINE_RE.match(value)
            if attribution is not None:
                title_part = _clean_title(attribution.group("title"))
                artist_part = _clean_artist(attribution.group("artist"))
            else:
                title_part = _clean_title(value)
        if not title_part:
            continue
        return title_part, artist_part, qualifier
    return None, None, None


def _description_qualifier(value: str) -> str | None:
    ranked = rank_transformations(detect_transformations(value))
    for item in ranked:
        if item in _DERIVATIVE_LABELS:
            return display_qualifier(item)
    return None


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


__all__ = ["NormalizedSourceIdentity", "looks_like_mix", "normalize_source_identity"]
