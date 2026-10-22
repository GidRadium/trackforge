"""Shared TrackForge test fixtures."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from trackforge.config import TrackForgeConfig


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
