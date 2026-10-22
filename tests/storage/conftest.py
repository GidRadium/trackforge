"""Fixtures for storage tests."""

from __future__ import annotations

import hashlib
import itertools
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from trackforge.adapters.persistence.filesystem.artifacts import FileSystemArtifactStore
from trackforge.adapters.persistence.filesystem.cas import FileSystemCas
from trackforge.adapters.persistence.sqlite.engine import Catalog, initialize_catalog
from trackforge.adapters.persistence.sqlite.unit_of_work import SqliteUnitOfWork
from trackforge.application.dto.artifacts import ArtifactManifest
from trackforge.application.services.canonical_json import canonical_json, sha256_hex
from trackforge.config import TrackForgeConfig
from trackforge.domain.model.claims import Claim, Subject
from trackforge.domain.model.ids import ItemId, new_uuid7
from trackforge.domain.model.item import Item, ItemStatus
from trackforge.domain.model.media import MediaContainer, MediaObject

FIXED_ARTIFACT_ID = "01930000-0000-7000-8000-000000000001"
FIXED_CREATED_AT = "2026-10-22T13:00:00Z"


@pytest.fixture
def cas(config: TrackForgeConfig) -> FileSystemCas:
    return FileSystemCas(data_dir=config.data_dir, cache_dir=config.cache_dir)


@pytest.fixture
def store(config: TrackForgeConfig) -> FileSystemArtifactStore:
    return FileSystemArtifactStore(data_dir=config.data_dir, cache_dir=config.cache_dir)


@pytest.fixture
def manifest_factory() -> Callable[..., ArtifactManifest]:
    def _make(**overrides: Any) -> ArtifactManifest:
        params = overrides.pop("params", {"b": 2, "a": "\u0451"})
        data: dict[str, Any] = {
            "artifact_id": FIXED_ARTIFACT_ID,
            "type": "audio.probe.v1",
            "schema_version": 1,
            "producer": {"id": "ffprobe", "version": "6.1"},
            "inputs": [],
            "params": params,
            "params_hash": sha256_hex(canonical_json(params)),
            "payload_hash": "",
            "warnings": [],
            "created_at": FIXED_CREATED_AT,
            "expires_at": None,
            "license": None,
        }
        data.update(overrides)
        return ArtifactManifest.model_validate(data)

    return _make


@pytest.fixture
def json_payload() -> bytes:
    return b'{"title":"Song","tags":["a","b"]}'


@pytest.fixture
def binary_payload() -> bytes:
    return bytes(range(256)) * 4


@pytest.fixture
def large_file(tmp_path: Path) -> Path:
    path = tmp_path / "large-input.bin"
    block = hashlib.sha256(b"trackforge-large-file").digest() * 32768
    with path.open("wb") as stream:
        for _ in range(10):
            stream.write(block)
    return path


@pytest.fixture
def catalog(config: TrackForgeConfig) -> Iterator[Catalog]:
    """An initialized catalog backed by a fresh temporary data directory."""

    value = initialize_catalog(config.data_dir)
    try:
        yield value
    finally:
        value.close()


@pytest.fixture
def uow_factory(catalog: Catalog) -> Callable[[], SqliteUnitOfWork]:
    """Factory for short-lived units of work over the test catalog."""

    return lambda: SqliteUnitOfWork(catalog)


@pytest.fixture
def make_media() -> Callable[..., MediaObject]:
    counter = itertools.count()

    def _make(**overrides: Any) -> MediaObject:
        index = next(counter)
        content_hash = hashlib.sha256(f"media-{index}".encode()).hexdigest()
        data: dict[str, Any] = {
            "id": new_uuid7(),
            "content_hash": content_hash,
            "size_bytes": 1024,
            "cas_path": f"cas/{content_hash[:2]}/{content_hash[2:4]}/{content_hash}",
            "container": MediaContainer.MP3,
            "codec": "mp3",
            "bitrate": 320000,
            "sample_rate": 44100,
            "channels": 2,
            "duration_seconds": 12.5,
        }
        data.update(overrides)
        return MediaObject(**data)

    return _make


@pytest.fixture
def make_item() -> Callable[..., Item]:
    def _make(**overrides: Any) -> Item:
        data: dict[str, Any] = {
            "id": new_uuid7(),
            "status": ItemStatus.DISCOVERED,
        }
        data.update(overrides)
        return Item(**data)

    return _make


@pytest.fixture
def make_claim() -> Callable[..., Claim]:
    def _make(item_id: ItemId, **overrides: Any) -> Claim:
        data: dict[str, Any] = {
            "id": new_uuid7(),
            "item_id": item_id,
            "subject": Subject.ITEM,
            "field": "title",
            "value": "Song",
            "source": "test",
            "method": "test_method/v1",
            "confidence": 0.9,
            "evidence": ("observation-1",),
        }
        data.update(overrides)
        return Claim(**data)

    return _make
