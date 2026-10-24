"""Shared validation and parsing helpers for metadata adapters."""

from __future__ import annotations

import re
from collections.abc import Iterable
from difflib import SequenceMatcher
from typing import Any

_MBID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_ISRC_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}[0-9]{7}$")
_ACOUSTID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")


def valid_mbid(value: object) -> str | None:
    """Return a lowercase MBID or ``None`` when the value is invalid."""

    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    return text if _MBID_RE.fullmatch(text) else None


def valid_isrc(value: object) -> str | None:
    """Return an uppercase ISRC or ``None`` when the value is invalid."""

    if not isinstance(value, str):
        return None
    text = value.strip().upper().replace("-", "")
    return text if _ISRC_RE.fullmatch(text) else None


def valid_acoustid(value: object) -> str | None:
    """Return a plausible AcoustID identifier or ``None``."""

    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if _ACOUSTID_RE.fullmatch(text) else None


def valid_mbids(values: Iterable[object]) -> list[str]:
    """Return valid, de-duplicated MBIDs preserving input order."""

    result: list[str] = []
    for value in values:
        candidate = valid_mbid(value)
        if candidate is not None and candidate not in result:
            result.append(candidate)
    return result


def valid_isrcs(values: Iterable[object]) -> list[str]:
    """Return valid, de-duplicated ISRCs preserving input order."""

    result: list[str] = []
    for value in values:
        candidate = valid_isrc(value)
        if candidate is not None and candidate not in result:
            result.append(candidate)
    return result


def clean_text(value: object) -> str | None:
    """Trim text and collapse whitespace; empty values become ``None``."""

    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def safe_float(value: object, *, minimum: float | None = None) -> float | None:
    """Coerce a JSON number without trusting provider formatting."""

    if isinstance(value, bool) or value is None:
        return None
    if not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in {float("inf"), float("-inf")}:
        return None
    if minimum is not None and number < minimum:
        return None
    return number


def safe_int(value: object, *, minimum: int | None = None) -> int | None:
    number = safe_float(value)
    if number is None:
        return None
    result = int(number)
    if minimum is not None and result < minimum:
        return None
    return result


def text_list(value: object) -> list[str]:
    """Normalize a string or list of strings into a clean list."""

    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    result: list[str] = []
    for item in items:
        text = clean_text(item)
        if text is not None and text not in result:
            result.append(text)
    return result


def duration_from_millis(value: object) -> float | None:
    """Convert a MusicBrainz duration in milliseconds into seconds."""

    millis = safe_float(value, minimum=0.0)
    return None if millis is None else millis / 1000.0


def score_fraction(value: object) -> float:
    """Normalize a provider score to the inclusive ``[0, 1]`` range."""

    number = safe_float(value, minimum=0.0)
    if number is None:
        return 0.0
    if number > 1.0:
        number /= 100.0
    return min(1.0, max(0.0, number))


def similarity(left: str | None, right: str | None) -> float:
    """Case-insensitive string similarity used only as evidence."""

    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left.casefold().strip(), right.casefold().strip()).ratio()


def mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def sequence(value: object) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


__all__ = [
    "clean_text",
    "duration_from_millis",
    "mapping",
    "safe_float",
    "safe_int",
    "score_fraction",
    "sequence",
    "similarity",
    "text_list",
    "valid_acoustid",
    "valid_isrc",
    "valid_isrcs",
    "valid_mbid",
    "valid_mbids",
]
