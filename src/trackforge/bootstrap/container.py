"""Composition root for TrackForge."""

from __future__ import annotations

from dataclasses import dataclass, field

from trackforge.adapters.persistence.filesystem.artifacts import FileSystemArtifactStore
from trackforge.adapters.persistence.filesystem.cas import FileSystemCas
from trackforge.config import TrackForgeConfig


@dataclass(frozen=True, slots=True)
class AppContext:
    """Explicit application dependencies assembled at the process boundary.

    Stores only capture their configured roots here; directories are created
    lazily on the first write.
    """

    config: TrackForgeConfig
    cas: FileSystemCas = field(init=False)
    artifact_store: FileSystemArtifactStore = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "cas", build_cas(self.config))
        object.__setattr__(self, "artifact_store", build_artifact_store(self.config))


def build_cas(config: TrackForgeConfig) -> FileSystemCas:
    """Build the content-addressed store for ``config`` without touching disk."""

    return FileSystemCas(data_dir=config.data_dir, cache_dir=config.cache_dir)


def build_artifact_store(config: TrackForgeConfig) -> FileSystemArtifactStore:
    """Build the artifact cache for ``config`` without touching disk."""

    return FileSystemArtifactStore(data_dir=config.data_dir, cache_dir=config.cache_dir)


def build_context(config: TrackForgeConfig) -> AppContext:
    """Build the application context from validated configuration."""

    return AppContext(config=config)


__all__ = [
    "AppContext",
    "build_artifact_store",
    "build_cas",
    "build_context",
]
