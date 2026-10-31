"""Resolve one collection cover to a CAS hash.

Playlist thumbnails are URLs; the artwork decision and filename must only ever
see a content hash.  The service fetches the first cover claim at most once per
collection, validates that the response really is a JPEG/PNG, stores it in CAS,
and returns the hash.  Failures are visible warnings, never run failures.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from trackforge.application.ports.storage import ContentAddressedStore, is_sha256_hex
from trackforge.domain.errors import OfflineModeError
from trackforge.domain.model.collection import Collection, CollectionContextClaim

MAX_COLLECTION_COVER_BYTES = 10 * 1024 * 1024

CoverFetcher = Callable[[str], Awaitable[bytes]]


class CollectionCoverStore(Protocol):
    async def put_bytes(self, data: bytes) -> str: ...


class CollectionCoverService:
    """Fetch, validate, and persist a playlist/collection cover once."""

    def __init__(
        self,
        *,
        cas: ContentAddressedStore | CollectionCoverStore | None = None,
        fetcher: CoverFetcher | None = None,
        fetch: CoverFetcher | None = None,
        store: CollectionCoverStore | None = None,
        max_bytes: int = MAX_COLLECTION_COVER_BYTES,
        enabled: bool = True,
    ) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        if fetcher is not None and fetch is not None:
            raise ValueError("pass either fetcher or fetch, not both")
        if cas is not None and store is not None:
            raise ValueError("pass either cas or store, not both")
        self._cas = cas if cas is not None else store
        self._fetcher = fetcher if fetcher is not None else fetch
        self._max_bytes = max_bytes
        self._enabled = bool(enabled)
        self._cache: dict[str, str] = {}
        self._warnings: tuple[str, ...] = ()

    @property
    def warnings(self) -> tuple[str, ...]:
        return self._warnings

    async def resolve(self, collection: Collection) -> str | None:
        """Return the CAS hash of the first usable cover claim, if any."""

        self._warnings = ()
        if not self._enabled:
            self._warnings = ("playlist_cover_disabled",)
            return None
        value = _first_cover_value(collection)
        if value is None:
            return None
        if is_sha256_hex(value):
            return value
        if not value.startswith(("http://", "https://")):
            self._warnings = ("playlist_cover_invalid_url",)
            return None
        cached = self._cache.get(value)
        if cached is not None:
            return cached
        if self._fetcher is None or self._cas is None:
            self._warnings = ("playlist_cover_unavailable",)
            return None
        try:
            data = await self._fetcher(value)
        except OfflineModeError:
            self._warnings = ("playlist_cover_offline",)
            return None
        except Exception:
            self._warnings = ("playlist_cover_fetch_failed",)
            return None
        if not data:
            self._warnings = ("playlist_cover_empty",)
            return None
        if len(data) > self._max_bytes:
            self._warnings = ("playlist_cover_too_large",)
            return None
        if not _is_supported_image(data):
            self._warnings = ("playlist_cover_not_image",)
            return None
        try:
            content_hash = str(await self._cas.put_bytes(data))
        except Exception:
            self._warnings = ("playlist_cover_store_failed",)
            return None
        self._cache[value] = content_hash
        return content_hash


def _first_cover_value(collection: Collection) -> str | None:
    fallback: str | None = None
    for claim in collection.context_claims:
        if claim.field.casefold() not in {"cover", "cover_url", "artwork"}:
            continue
        value = claim.value
        if not isinstance(value, str) or not value.strip():
            continue
        text = value.strip()
        if is_sha256_hex(text):
            return text
        if fallback is None:
            fallback = text
    return fallback


def _is_supported_image(data: bytes) -> bool:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return len(data) > 8
    if data.startswith(b"\xff\xd8"):
        return len(data) > 2
    return False


def cover_context_claim(content_hash: str) -> CollectionContextClaim:
    """Build the item-context claim carrying a downloaded playlist cover."""

    return CollectionContextClaim(
        field="artwork",
        value=content_hash,
        source="playlist_cover",
        confidence=0.7,
    )


__all__ = [
    "MAX_COLLECTION_COVER_BYTES",
    "CollectionCoverService",
    "CollectionCoverStore",
    "CoverFetcher",
    "cover_context_claim",
]
