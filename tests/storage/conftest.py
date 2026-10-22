"""Fixtures for storage tests."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from trackforge.adapters.persistence.filesystem.artifacts import FileSystemArtifactStore
from trackforge.adapters.persistence.filesystem.cas import FileSystemCas
from trackforge.application.dto.artifacts import ArtifactManifest
from trackforge.application.services.canonical_json import canonical_json, sha256_hex
from trackforge.config import TrackForgeConfig

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
