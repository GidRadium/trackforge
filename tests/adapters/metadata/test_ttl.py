"""TTL behavior for external observation artifacts: stale use and refresh."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trackforge.adapters.persistence.filesystem.artifacts import FileSystemArtifactStore
from trackforge.application.ports.execution import ArtifactDraft, StepResult
from trackforge.application.services.canonical_json import sha256_hex
from trackforge.domain.model.capabilities import ArtifactType, CapabilityKind, CapabilityManifest
from trackforge.pipeline.cache import Cache

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
MB = ArtifactType.parse("musicbrainz.search.v1")


def _manifest() -> CapabilityManifest:
    return CapabilityManifest(
        id="musicbrainz_search",
        version="1",
        kind=CapabilityKind.OBSERVER,
        provides=(MB,),
    )


async def test_expired_external_artifact_is_returned_as_stale(
    tmp_path: Path,
) -> None:
    store = FileSystemArtifactStore(tmp_path / "data", tmp_path / "cache")
    cache = Cache(store, clock=lambda: NOW)
    manifest = _manifest()
    key = sha256_hex(b"musicbrainz fixture cache key")
    expired = StepResult(
        artifacts=(
            ArtifactDraft(
                type=MB,
                payload={"recordings": [], "releases": [], "artists": []},
                expires_at=NOW - timedelta(days=1),
            ),
        )
    )
    await cache.store(key, expired, producer=manifest)

    # Normal lookup treats the expired artifact as a miss.
    assert await cache.lookup(key, 1) is None

    ref, stale = await cache.lookup_with_status(key, 1, allow_stale=True)
    assert ref is not None
    assert stale is True
    entry = await cache.load(ref, 1)
    assert entry.result.artifacts[0].payload == {
        "recordings": [],
        "releases": [],
        "artists": [],
    }


async def test_refresh_ignores_ttl_but_force_skips_lookup(tmp_path: Path) -> None:
    store = FileSystemArtifactStore(tmp_path / "data", tmp_path / "cache")
    cache = Cache(store, clock=lambda: NOW)
    manifest = _manifest()
    key = sha256_hex(b"refresh cache key")
    expired = StepResult(
        artifacts=(
            ArtifactDraft(
                type=MB,
                payload={"recordings": []},
                expires_at=NOW - timedelta(days=1),
            ),
        )
    )
    await cache.store(key, expired, producer=manifest)

    ref, stale = await cache.lookup_with_status(key, 1, refresh=True)
    assert ref is not None
    assert stale is False

    forced, _ = await cache.lookup_with_status(key, 1, force=True)
    assert forced is None


async def test_cached_external_manifest_never_contains_secrets(tmp_path: Path) -> None:
    store = FileSystemArtifactStore(tmp_path / "data", tmp_path / "cache")
    cache = Cache(store, clock=lambda: NOW)
    manifest = _manifest()
    key = sha256_hex(b"secret free cache key")
    result = StepResult(
        artifacts=(
            ArtifactDraft(
                type=MB,
                payload={"recordings": []},
                params={"provider": "musicbrainz"},
                expires_at=NOW + timedelta(days=7),
            ),
        )
    )
    await cache.store(
        key,
        result,
        producer=manifest,
        params={"query": "recording:fixture"},
        inputs=[],
    )
    ref = await store.find(key)
    assert ref is not None
    stored, payload = await store.load(ref)
    serialized = stored.model_dump_json() + payload.decode("utf-8")
    assert "api_key" not in serialized
    assert "Authorization" not in serialized
    assert "cookie" not in serialized.lower()


async def test_refreshed_payload_is_stored_under_a_sibling_key(tmp_path: Path) -> None:
    store = FileSystemArtifactStore(tmp_path / "data", tmp_path / "cache")
    cache = Cache(store, clock=lambda: NOW)
    manifest = _manifest()
    key = sha256_hex(b"refresh conflict key")
    first = StepResult(
        artifacts=(ArtifactDraft(type=MB, payload={"revision": 1}),),
    )
    second = StepResult(
        artifacts=(ArtifactDraft(type=MB, payload={"revision": 2}),),
    )
    first_refs = await cache.store(key, first, producer=manifest)
    second_refs = await cache.store(key, second, producer=manifest)

    assert first_refs[0].cache_key == key
    assert second_refs[0].cache_key != key
    # The old artifact remains available under its original key.
    old_ref = await cache.lookup(key, 1)
    assert old_ref is not None
    old_entry = await cache.load(old_ref, 1)
    assert old_entry.result.artifacts[0].payload == {"revision": 1}
    new_entry = await cache.load(second_refs[0], 1)
    assert new_entry.result.artifacts[0].payload == {"revision": 2}
