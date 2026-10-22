"""Shared TrackForge test fixtures."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from trackforge.application.dto.db import LockRecord
from trackforge.config import TrackForgeConfig
from trackforge.domain.model.claims import Claim, Observation, Subject
from trackforge.domain.model.decisions import FieldPolicy, PolicyStrategy
from trackforge.domain.model.ids import ItemId, ObservationId, new_uuid7

NOW = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove ambient TrackForge variables for deterministic configuration."""

    for name in tuple(os.environ):
        if name.upper().startswith("TRACKFORGE_"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def config(tmp_path: Path) -> TrackForgeConfig:
    """An explicit, fully isolated configuration rooted in ``tmp_path``."""

    return TrackForgeConfig(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
        library_dir=tmp_path / "library",
    )


@pytest.fixture
def toml_config_path(tmp_path: Path) -> Path:
    """A TOML file using the documented ``[core]`` table."""

    path = tmp_path / "config.toml"
    path.write_text('[core]\nprofile = "fast"\n', encoding="utf-8")
    return path


@pytest.fixture
def make_observation() -> Callable[..., Observation]:
    """Build an immutable observation with sensible provenance defaults."""

    def _make(**overrides: Any) -> Observation:
        data: dict[str, Any] = {
            "id": ObservationId(new_uuid7()),
            "item_id": ItemId(new_uuid7()),
            "type": "audio.probe",
            "schema_version": 1,
            "producer": "fixture/v1",
            "created_at": NOW,
        }
        data.update(overrides)
        return Observation(**data)

    return _make


@pytest.fixture
def make_claim() -> Callable[..., Claim]:
    """Build a claim for policy and resolution tests."""

    def _make(item_id: ItemId | None = None, **overrides: Any) -> Claim:
        data: dict[str, Any] = {
            "id": new_uuid7(),
            "item_id": item_id or ItemId(new_uuid7()),
            "subject": Subject.ITEM,
            "field": "title",
            "value": "Song",
            "source": "fixture",
            "method": "fixture/v1",
            "confidence": 0.9,
            "evidence": ("artifact-1",),
            "created_at": NOW,
        }
        data.update(overrides)
        return Claim(**data)

    return _make


@pytest.fixture
def make_lock() -> Callable[..., LockRecord]:
    """Build a lock DTO for resolver tests."""

    def _make(item_id: ItemId | None = None, **overrides: Any) -> LockRecord:
        data: dict[str, Any] = {
            "id": new_uuid7(),
            "item_id": item_id or ItemId(new_uuid7()),
            "field": "title",
            "value": "Locked",
            "created_at": NOW,
            "updated_at": NOW,
        }
        data.update(overrides)
        return LockRecord(**data)

    return _make


@pytest.fixture
def make_policy() -> Callable[..., FieldPolicy]:
    """Build a versioned field policy for strategy tests."""

    def _make(**overrides: Any) -> FieldPolicy:
        data: dict[str, Any] = {
            "id": "title",
            "field": "title",
            "strategy": PolicyStrategy.SOURCE_PRIORITY,
            "priority": ("user_lock", "item.title", "path.title", "unknown"),
            "version": 1,
        }
        data.update(overrides)
        return FieldPolicy(**data)

    return _make
