"""Unit tests for collection cover fetch/CAS resolution."""

from __future__ import annotations

import hashlib

import pytest

from trackforge.application.services.collection_cover import CollectionCoverService
from trackforge.domain.model.collection import (
    Collection,
    CollectionContextClaim,
    CollectionKind,
)
from trackforge.domain.model.ids import CollectionId, new_uuid7
from trackforge.domain.model.source import Url

pytestmark = pytest.mark.unit

PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x10\x00\x00\x00\x10\x08\x02\x00\x00\x00"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


class FakeCas:
    def __init__(self) -> None:
        self.objects: list[bytes] = []

    async def put_bytes(self, data: bytes) -> str:
        self.objects.append(data)
        return hashlib.sha256(data).hexdigest()


def _collection(*claims: CollectionContextClaim) -> Collection:
    return Collection(
        id=CollectionId(new_uuid7()),
        kind=CollectionKind.PLAYLIST,
        source_ref=Url("https://example.test/playlist"),
        title="Fixture Playlist",
        context_claims=claims,
        items=(),
    )


def _cover_claim(value: str) -> CollectionContextClaim:
    return CollectionContextClaim(field="cover", value=value, source="fixture", confidence=0.6)


@pytest.mark.asyncio
async def test_playlist_cover_is_fetched_once_and_stored_in_cas() -> None:
    calls: list[str] = []
    cas = FakeCas()

    async def fetch(url: str) -> bytes:
        calls.append(url)
        return PNG

    service = CollectionCoverService(cas=cas, fetcher=fetch)
    collection = _collection(_cover_claim("https://example.test/cover.png"))

    first = await service.resolve(collection)
    second = await service.resolve(collection)

    assert first == hashlib.sha256(PNG).hexdigest()
    assert second == first
    assert calls == ["https://example.test/cover.png"]
    assert cas.objects == [PNG]
    assert service.warnings == ()


@pytest.mark.asyncio
async def test_cover_failure_is_a_warning_and_none() -> None:
    async def fetch(url: str) -> bytes:
        raise RuntimeError("offline")

    service = CollectionCoverService(cas=FakeCas(), fetcher=fetch)
    result = await service.resolve(_collection(_cover_claim("https://example.test/cover.jpg")))

    assert result is None
    assert "playlist_cover_fetch_failed" in service.warnings


@pytest.mark.asyncio
async def test_non_image_payload_is_rejected_before_cas() -> None:
    cas = FakeCas()

    async def fetch(url: str) -> bytes:
        return b"not an image"

    service = CollectionCoverService(cas=cas, fetcher=fetch)
    result = await service.resolve(_collection(_cover_claim("https://example.test/cover.jpg")))

    assert result is None
    assert "playlist_cover_not_image" in service.warnings
    assert cas.objects == []


@pytest.mark.asyncio
async def test_existing_hash_context_does_not_fetch() -> None:
    calls = 0

    async def fetch(url: str) -> bytes:
        nonlocal calls
        calls += 1
        return PNG

    content_hash = "a" * 64
    service = CollectionCoverService(cas=FakeCas(), fetcher=fetch)
    result = await service.resolve(_collection(_cover_claim(content_hash)))

    assert result == content_hash
    assert calls == 0


@pytest.mark.asyncio
async def test_disabled_cover_service_returns_none() -> None:
    async def fetch(url: str) -> bytes:
        raise AssertionError("must not fetch")

    service = CollectionCoverService(cas=FakeCas(), fetcher=fetch, enabled=False)
    assert (
        await service.resolve(_collection(_cover_claim("https://example.test/cover.jpg"))) is None
    )
    assert "playlist_cover_disabled" in service.warnings
