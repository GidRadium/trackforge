"""LRCLIB plain and LRC lyrics provider.

LRCLIB is free and does not require authentication.  Candidates are returned
as observations; this module never merges ASR text and never chooses final
lyrics.  LRC is parsed and validated, with malformed or non-monotonic timings
kept visible as warnings rather than silently dropping the candidate.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from trackforge.adapters.http.client import HttpClient
from trackforge.adapters.metadata.common import (
    clean_text,
    mapping,
    safe_float,
    similarity,
)
from trackforge.application.dto.providers import (
    LyricsCandidate,
    LyricsCandidatesPayload,
    MetadataQuery,
    ProviderObservation,
    ttl_expiry,
)
from trackforge.domain.errors import ProviderUnavailable

DEFAULT_BASE_URL = "https://lrclib.net/api"
DEFAULT_TTL = timedelta(days=7)
PROVIDER_VERSION = "1"
MAX_LYRICS_BYTES = 256 * 1024

_LRC_TIMESTAMP_RE = re.compile(
    r"\[(?:(?P<hours>\d{1,2}):)?(?P<minutes>\d{1,3}):(?P<seconds>\d{1,2}(?:\.\d{1,3})?)\]"
)
_LRC_META_RE = re.compile(r"^\[(?P<key>[a-zA-Z]+):(?P<value>[^\]]*)\]$")


def parse_lrc(value: str) -> tuple[tuple[tuple[float, str], ...], tuple[str, ...]]:
    """Parse LRC text into ``(seconds, text)`` pairs and warning codes.

    Warnings include ``lrc_invalid_timestamp`` and ``lrc_timings_not_increasing``.
    A candidate with any warning should not be treated as reliably synced.
    """

    lines: list[tuple[float, str]] = []
    warnings: list[str] = []
    previous = -1.0
    for raw_line in value.splitlines():
        text = raw_line.strip()
        if not text:
            continue
        if _LRC_META_RE.match(text) and not _LRC_TIMESTAMP_RE.search(text):
            continue
        stamps = list(_LRC_TIMESTAMP_RE.finditer(text))
        if not stamps:
            warnings.append("lrc_invalid_timestamp")
            continue
        remainder = _LRC_TIMESTAMP_RE.sub("", text).strip()
        if not remainder:
            continue
        for match in stamps:
            hours = int(match.group("hours") or 0)
            minutes = int(match.group("minutes"))
            seconds = float(match.group("seconds"))
            if minutes >= 60 or seconds >= 60.0:
                warnings.append("lrc_invalid_timestamp")
                continue
            position = hours * 3600 + minutes * 60 + seconds
            if position < 0:
                warnings.append("lrc_invalid_timestamp")
                continue
            if position <= previous:
                warnings.append("lrc_timings_not_increasing")
            previous = max(previous, position)
            lines.append((position, remainder))
    if not lines:
        warnings.append("lrc_empty")
    return tuple(lines), tuple(dict.fromkeys(warnings))


def _language_hint(text: str | None) -> str | None:
    if not text:
        return None
    if re.search(r"[\u0400-\u04FF]", text):
        return "ru"
    if re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", text):
        return "ja" if re.search(r"[\u3040-\u30ff]", text) else "zh"
    if re.search(r"[\uac00-\ud7af]", text):
        return "ko"
    return None


class LrclibProvider:
    """Search and fetch lyrics candidates from LRCLIB."""

    id = "lrclib"
    name = "LRCLIB"
    version = PROVIDER_VERSION
    source = "lrclib"

    def __init__(
        self,
        client: HttpClient | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        clock: Callable[[], datetime] | None = None,
        ttl: timedelta = DEFAULT_TTL,
        search_fallback: bool = False,
    ) -> None:
        self._client = client
        self._owns_client = client is None
        self._base_url = base_url.rstrip("/")
        self._clock = clock
        self._ttl = ttl
        self._search_fallback = search_fallback

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _effective_client(self) -> HttpClient:
        if self._client is None:
            self._client = HttpClient()
        return self._client

    def _expires(self) -> datetime:
        from trackforge.domain.model._validation import utc_now

        return ttl_expiry(self._ttl, now=self._clock() if self._clock else utc_now())

    async def search(
        self,
        query: MetadataQuery | Mapping[str, Any],
    ) -> Sequence[ProviderObservation]:
        normalized = MetadataQuery.coerce(query)
        if not normalized.title or not normalized.artist:
            return [self._observation([], ("no_query",))]
        params: dict[str, str] = {
            "track_name": normalized.title,
            "artist_name": normalized.artist,
        }
        if normalized.album:
            params["album_name"] = normalized.album
        if normalized.duration is not None:
            params["duration"] = str(max(0, int(round(normalized.duration))))
        response = await self._effective_client().get(f"{self._base_url}/get", params=params)
        if response.status_code == 404:
            if self._search_fallback:
                return await self._search_legacy(normalized)
            return [self._observation([], ("no_match",))]
        if not response.ok:
            response.raise_for_status()
        document = response.json()
        if not isinstance(document, Mapping):
            raise ProviderUnavailable(
                "LRCLIB returned a non-object response",
                retryable=False,
                context={"url": response.url},
            )
        if "message" in document and not document.get("trackName"):
            return [self._observation([], ("no_match",))]
        candidate, warnings = self._candidate(document, normalized)
        return [self._observation([candidate] if candidate else [], warnings)]

    async def _search_legacy(self, query: MetadataQuery) -> Sequence[ProviderObservation]:
        response = await self._effective_client().get(
            f"{self._base_url}/search",
            params={
                "track_name": query.title or "",
                "artist_name": query.artist or "",
            },
        )
        if response.status_code == 404:
            return [self._observation([], ("no_match",))]
        if not response.ok:
            response.raise_for_status()
        document = response.json()
        items = document if isinstance(document, list) else []
        candidates: list[LyricsCandidate] = []
        warnings: list[str] = []
        for raw in items[: query.limit]:
            candidate, item_warnings = self._candidate(mapping(raw), query)
            warnings.extend(item_warnings)
            if candidate is not None:
                candidates.append(candidate)
        return [self._observation(candidates, warnings)]

    async def lookup(self, identifier: str) -> ProviderObservation:
        text = str(identifier).strip()
        if not text.isdigit():
            raise ProviderUnavailable(
                "invalid LRCLIB identifier",
                retryable=False,
                context={"identifier": identifier},
            )
        response = await self._effective_client().get(f"{self._base_url}/get/{text}")
        if response.status_code == 404:
            return self._observation([], ("no_match",))
        if not response.ok:
            response.raise_for_status()
        document = response.json()
        if not isinstance(document, Mapping):
            raise ProviderUnavailable(
                "LRCLIB returned a non-object response",
                retryable=False,
                context={"url": response.url},
            )
        candidate, warnings = self._candidate(
            document,
            MetadataQuery(
                title=clean_text(document.get("trackName")),
                artist=clean_text(document.get("artistName")),
            ),
        )
        return self._observation([candidate] if candidate else [], warnings)

    def _candidate(
        self,
        document: Mapping[str, Any],
        query: MetadataQuery,
    ) -> tuple[LyricsCandidate | None, tuple[str, ...]]:
        warnings: list[str] = []
        title = clean_text(document.get("trackName")) or query.title
        artist = clean_text(document.get("artistName")) or query.artist
        if not title or not artist:
            return None, ("lyrics_missing_title_or_artist",)
        if bool(document.get("instrumental")):
            return None, ("instrumental",)
        plain = _lyrics_text(document.get("plainLyrics"))
        lrc = _lyrics_text(document.get("syncedLyrics"))
        has_timing = False
        if lrc is not None:
            lines, lrc_warnings = parse_lrc(lrc)
            warnings.extend(lrc_warnings)
            has_timing = bool(lines) and not any(
                warning.startswith("lrc_") for warning in lrc_warnings
            )
        if plain is None and lrc is None:
            return None, ("no_lyrics",)
        duration = safe_float(document.get("duration"), minimum=0.0)
        if query.duration is not None and duration is not None:
            delta = abs(query.duration - duration)
            duration_score = max(0.0, 1.0 - delta / 15.0)
        elif duration is not None:
            duration_score = 0.5
        else:
            duration_score = 0.0
        score = (
            0.45 * similarity(title, query.title)
            + 0.25 * similarity(artist, query.artist)
            + 0.2 * duration_score
            + (0.1 if has_timing else 0.0)
        )
        candidate = LyricsCandidate(
            source=self.source,
            title=title,
            artist=artist,
            album=clean_text(document.get("albumName")) or query.album,
            duration=duration,
            plain=plain,
            lrc=lrc,
            language=_language_hint(plain or lrc),
            has_timing=has_timing,
            score=max(0.0, min(1.0, score)),
        )
        return candidate, tuple(dict.fromkeys(warnings))

    def _observation(
        self,
        candidates: Sequence[LyricsCandidate],
        warnings: Sequence[str],
    ) -> ProviderObservation:
        payload = LyricsCandidatesPayload(
            candidates=list(candidates),
            warnings=list(dict.fromkeys(warnings)),
        )
        return ProviderObservation(
            type="lyrics.candidates.v1",
            payload=payload.model_dump(mode="json"),
            source=self.source,
            provider_version=self.version,
            warnings=tuple(dict.fromkeys(warnings)),
            expires_at=self._expires(),
        )


def _lyrics_text(value: object) -> str | None:
    if value is None:
        return None
    # Preserve line breaks: both plain lyrics and LRC timings are line-based.
    text = str(value).strip()
    if not text:
        return None
    if len(text.encode("utf-8")) > MAX_LYRICS_BYTES:
        return text.encode("utf-8")[:MAX_LYRICS_BYTES].decode("utf-8", errors="ignore")
    return text


# Common capitalization used in documentation.
LRCLIBProvider = LrclibProvider


def __getattr__(name: str) -> Any:
    if name in {"LrclibObserver", "LRCLIBObserver", "LyricsObserver"}:
        from trackforge.adapters.metadata.observers import LrclibObserver

        return LrclibObserver
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_TTL",
    "LRCLIBProvider",
    "LrclibProvider",
    "MAX_LYRICS_BYTES",
    "parse_lrc",
]
