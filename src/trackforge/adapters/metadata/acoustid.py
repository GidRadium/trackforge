"""AcoustID fingerprint lookup provider.

The provider is intentionally thin: Chromaprint produces the fingerprint and
AcoustID returns scored recording candidates.  The API key is read from
``TRACKFORGE_ACOUSTID_API_KEY`` (or an explicit constructor argument) and is
never included in observation payloads, cache keys, or error messages.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from trackforge.adapters.http.client import HttpClient
from trackforge.adapters.metadata.common import (
    clean_text,
    mapping,
    score_fraction,
    sequence,
    valid_acoustid,
    valid_mbids,
)
from trackforge.application.dto.providers import (
    AcoustIdCandidate,
    AcoustIdLookupPayload,
    MetadataQuery,
    ProviderObservation,
    ttl_expiry,
)
from trackforge.domain.errors import DomainValidationError, ProviderUnavailable

DEFAULT_BASE_URL = "https://api.acoustid.org/v2/lookup"
DEFAULT_TTL = timedelta(days=30)
PROVIDER_VERSION = "1"
_API_KEY_ENV = "TRACKFORGE_ACOUSTID_API_KEY"


class AcoustIdProvider:
    """Look up Chromaprint fingerprints through AcoustID."""

    id = "acoustid"
    name = "AcoustID"
    version = PROVIDER_VERSION
    source = "acoustid"

    def __init__(
        self,
        client: HttpClient | None = None,
        *,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        clock: Callable[[], datetime] | None = None,
        ttl: timedelta = DEFAULT_TTL,
        min_score: float = 0.0,
    ) -> None:
        self._client = client
        self._owns_client = client is None
        self._api_key = (
            api_key if api_key is not None else os.environ.get(_API_KEY_ENV, "")
        ).strip()
        self._base_url = base_url
        self._clock = clock
        self._ttl = ttl
        self._min_score = max(0.0, min(1.0, float(min_score)))

    @property
    def api_key_configured(self) -> bool:
        return bool(self._api_key)

    @property
    def configured(self) -> bool:
        return self.api_key_configured

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
        if not normalized.fingerprint:
            raise ProviderUnavailable(
                "AcoustID lookup requires an audio fingerprint",
                retryable=False,
                user_action="Enable fingerprinting (install fpcalc) and retry.",
            )
        return [
            await self._lookup(
                normalized.fingerprint,
                normalized.duration,
                acoustid_id=normalized.acoustid_id,
            )
        ]

    async def lookup(
        self,
        identifier: str,
        *,
        duration: float | None = None,
    ) -> ProviderObservation:
        """Look up a fingerprint.

        ``identifier`` is the Chromaprint fingerprint.  Use :meth:`search` when
        a duration is available; AcoustID requires it for accurate matching.
        """

        return await self._lookup(identifier, duration)

    async def _lookup(
        self,
        fingerprint: str,
        duration: float | None,
        *,
        acoustid_id: str | None = None,
    ) -> ProviderObservation:
        if not self._api_key:
            raise ProviderUnavailable(
                "AcoustID API key is not configured",
                retryable=False,
                user_action="Set TRACKFORGE_ACOUSTID_API_KEY to enable AcoustID.",
            )
        if not fingerprint.strip():
            raise DomainValidationError("fingerprint must not be empty")
        params: dict[str, str] = {
            "client": self._api_key,
            "format": "json",
            "meta": "recordings+releasegroups+compress",
            "fingerprint": fingerprint.strip(),
        }
        if duration is not None:
            params["duration"] = str(max(0, int(round(duration))))
        if acoustid_id:
            params["trackid"] = acoustid_id
        response = await self._effective_client().get(self._base_url, params=params)
        if not response.ok:
            response.raise_for_status()
        document = response.json()
        if not isinstance(document, Mapping):
            raise ProviderUnavailable(
                "AcoustID returned a non-object response",
                retryable=False,
                context={"url": response.url},
            )
        status = clean_text(document.get("status"))
        if status not in {None, "ok"}:
            raise ProviderUnavailable(
                f"AcoustID returned status {status}",
                retryable=status in {"error", "fail"},
                context={"url": response.url, "status": status},
            )
        candidates, warnings = _candidates(document, self._min_score)
        payload = AcoustIdLookupPayload(candidates=candidates)
        return ProviderObservation(
            type="acoustid.lookup.v1",
            payload=payload.model_dump(mode="json"),
            source=self.source,
            provider_version=self.version,
            warnings=tuple(warnings),
            expires_at=self._expires(),
        )


def _candidates(
    document: Mapping[str, Any],
    min_score: float,
) -> tuple[list[AcoustIdCandidate], tuple[str, ...]]:
    candidates: list[AcoustIdCandidate] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for raw in sequence(document.get("results")):
        result = mapping(raw)
        acoustid_id = valid_acoustid(result.get("id"))
        score = score_fraction(result.get("score"))
        if acoustid_id is None:
            warnings.append("invalid_acoustid_id")
            continue
        if score < min_score:
            continue
        recordings = sequence(result.get("recordings"))
        mbids: list[str] = []
        title: str | None = None
        artist: str | None = None
        for raw_recording in recordings:
            recording = mapping(raw_recording)
            mbid = valid_mbids([recording.get("id")])
            if recording.get("id") is not None and not mbid:
                warnings.append("invalid_recording_mbid")
            for value in mbid:
                if value not in mbids:
                    mbids.append(value)
            title = title or clean_text(recording.get("title"))
            if artist is None:
                cleaned_names: list[str] = []
                for credit in sequence(recording.get("artists")):
                    name = clean_text(mapping(credit).get("name"))
                    if name is not None:
                        cleaned_names.append(name)
                if cleaned_names:
                    artist = ", ".join(cleaned_names)
        key = acoustid_id
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            AcoustIdCandidate(
                acoustid_id=acoustid_id,
                recording_mbids=mbids,
                score=score,
                title=title,
                artist=artist,
            )
        )
    candidates.sort(key=lambda candidate: candidate.score, reverse=True)
    return candidates, tuple(dict.fromkeys(warnings))


# Compatibility alias: the adapter is often referred to without the ID casing.
AcoustIDProvider = AcoustIdProvider


def __getattr__(name: str) -> Any:
    if name in {"AcoustIdObserver", "AcoustIDObserver"}:
        from trackforge.adapters.metadata.observers import AcoustIdObserver

        return AcoustIdObserver
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "AcoustIDProvider",
    "AcoustIdProvider",
    "DEFAULT_BASE_URL",
    "DEFAULT_TTL",
]
