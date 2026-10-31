"""Clean and bound genre/tag lists supplied by a source.

YouTube categories are not genres: values such as ``Music``,
``Lyrics (Website Category)`` or ``Drama (TV Genre)`` describe the hosting
site, not the recording.  This module keeps the useful source tags and applies
a hard cardinality limit so a noisy playlist cannot bloat the final genre tag.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

MAX_SOURCE_GENRES = 6
MAX_GENRE_LENGTH = 40

_WEBSITE_CATEGORY_RE = re.compile(r"\(\s*website\s+category\s*\)\s*$", re.IGNORECASE)
_TV_GENRE_RE = re.compile(r"\(\s*tv\s+genre\s*\)\s*$", re.IGNORECASE)
_FILM_GENRE_RE = re.compile(r"\(\s*film\s+genre\s*\)\s*$", re.IGNORECASE)
_URL_RE = re.compile(r"^(?:https?://|www\.)\S+$", re.IGNORECASE)
_GENERIC = frozenset(
    {
        "music",
        "lyrics",
        "lyric",
        "song",
        "songs",
        "audio",
        "video",
        "hd",
        "hq",
        "4k",
        "8k",
        "uhd",
        "official",
        "website category",
        "tv genre",
        "film genre",
    }
)


def normalize_source_genres(
    tags: Iterable[str] | str | None,
    title: str | None = None,
    uploader: str | None = None,
) -> tuple[str, ...]:
    """Return at most six cleaned, case-insensitively unique source genres."""

    values = _as_items(tags)
    title_key = _key(title) if title else ""
    uploader_key = _key(uploader) if uploader else ""
    title_tokens = _tokens(title) if title else frozenset()
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = _clean(raw)
        if value is None:
            continue
        key = value.casefold()
        if key in seen:
            continue
        if len(value) > MAX_GENRE_LENGTH:
            continue
        if key in _GENERIC:
            continue
        if _WEBSITE_CATEGORY_RE.search(value):
            continue
        if _TV_GENRE_RE.search(value) or _FILM_GENRE_RE.search(value):
            continue
        if "(website category)" in key or "(tv genre)" in key or "(film genre)" in key:
            continue
        if _URL_RE.match(value) or "http://" in key or "https://" in key:
            continue
        if uploader_key and key == uploader_key:
            continue
        if (
            uploader_key
            and len(uploader_key) >= 6
            and (len(key) >= 6 or " " in key)
            and key in uploader_key
        ):
            continue
        if title_key and key == title_key:
            continue
        if title_tokens and _is_title_fragment(value, title_tokens):
            continue
        seen.add(key)
        result.append(value)
        if len(result) >= MAX_SOURCE_GENRES:
            break
    return tuple(result)


def _as_items(tags: Iterable[str] | str | None) -> tuple[object, ...]:
    if tags is None:
        return ()
    if isinstance(tags, str):
        return (tags,)
    try:
        return tuple(tags)
    except TypeError:
        return ()


def _clean(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = unicodedata.normalize("NFC", value)
    text = " ".join(text.split()).strip(" \t,;|/")
    return text or None


def _key(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split()).casefold()


def _tokens(value: str) -> frozenset[str]:
    return frozenset(
        token
        for token in re.split(r"[^\w]+", unicodedata.normalize("NFC", value).casefold())
        if token
    )


def _is_title_fragment(value: str, title_tokens: frozenset[str]) -> bool:
    value_tokens = _tokens(value)
    if not value_tokens:
        return True
    return value_tokens <= title_tokens


__all__ = [
    "MAX_GENRE_LENGTH",
    "MAX_SOURCE_GENRES",
    "normalize_source_genres",
]
