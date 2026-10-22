"""Composition root for TrackForge."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import TracebackType

from trackforge.adapters.persistence.filesystem.artifacts import FileSystemArtifactStore
from trackforge.adapters.persistence.filesystem.cas import FileSystemCas
from trackforge.adapters.persistence.sqlite.engine import Catalog, initialize_catalog
from trackforge.adapters.persistence.sqlite.file_lock import FileLock
from trackforge.adapters.persistence.sqlite.unit_of_work import SqliteUnitOfWork
from trackforge.config import TrackForgeConfig


@dataclass(frozen=True, slots=True)
class AppContext:
    """Explicit application dependencies assembled at the process boundary.

    Stores only capture their configured roots here; directories are created
    lazily on the first write.  The catalog is opened explicitly through
    :meth:`open_catalog` so read-only commands (for example ``config show``)
    never touch the data directory.
    """

    config: TrackForgeConfig
    cas: FileSystemCas = field(init=False)
    artifact_store: FileSystemArtifactStore = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "cas", build_cas(self.config))
        object.__setattr__(self, "artifact_store", build_artifact_store(self.config))

    def open_catalog(self) -> CatalogContext:
        """Create and migrate the catalog for this configuration."""

        return build_catalog_context(self.config)


@dataclass(slots=True)
class CatalogContext:
    """An initialized catalog, its unit-of-work factory, and data-dir lock."""

    config: TrackForgeConfig
    catalog: Catalog
    lock: FileLock = field(init=False)

    def __post_init__(self) -> None:
        self.lock = FileLock(self.config.data_dir)

    def unit_of_work(self) -> SqliteUnitOfWork:
        """Create a fresh short-lived unit of work."""

        return SqliteUnitOfWork(self.catalog)

    def close(self) -> None:
        """Release the lock and dispose the catalog engine."""

        try:
            self.lock.release()
        finally:
            self.catalog.close()

    def __enter__(self) -> CatalogContext:
        self.lock.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    async def __aenter__(self) -> CatalogContext:
        self.lock.acquire()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def build_cas(config: TrackForgeConfig) -> FileSystemCas:
    """Build the content-addressed store for ``config`` without touching disk."""

    return FileSystemCas(data_dir=config.data_dir, cache_dir=config.cache_dir)


def build_artifact_store(config: TrackForgeConfig) -> FileSystemArtifactStore:
    """Build the artifact cache for ``config`` without touching disk."""

    return FileSystemArtifactStore(data_dir=config.data_dir, cache_dir=config.cache_dir)


def build_context(config: TrackForgeConfig) -> AppContext:
    """Build the application context from validated configuration."""

    return AppContext(config=config)


def build_catalog(config: TrackForgeConfig) -> Catalog:
    """Initialize (creating if needed) the configured SQLite catalog."""

    return initialize_catalog(config.data_dir)


def build_catalog_context(config: TrackForgeConfig) -> CatalogContext:
    """Initialize the catalog and return a lockable catalog context."""

    return CatalogContext(config=config, catalog=build_catalog(config))


def build_unit_of_work(catalog: Catalog) -> SqliteUnitOfWork:
    """Build a fresh unit of work over an existing catalog."""

    return SqliteUnitOfWork(catalog)


__all__ = [
    "AppContext",
    "CatalogContext",
    "build_artifact_store",
    "build_cas",
    "build_catalog",
    "build_catalog_context",
    "build_context",
    "build_unit_of_work",
]
