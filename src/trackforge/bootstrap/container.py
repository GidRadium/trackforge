"""Composition root for TrackForge."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from types import TracebackType

from trackforge.adapters.acquisition.local_copy import LocalCopyAcquirer
from trackforge.adapters.analysis.ffprobe import FfprobeProbe
from trackforge.adapters.analysis.path_structure import PathStructureObserver
from trackforge.adapters.persistence.filesystem.artifacts import FileSystemArtifactStore
from trackforge.adapters.persistence.filesystem.cas import FileSystemCas
from trackforge.adapters.persistence.sqlite.engine import Catalog, initialize_catalog
from trackforge.adapters.persistence.sqlite.file_lock import FileLock
from trackforge.adapters.persistence.sqlite.unit_of_work import SqliteUnitOfWork
from trackforge.adapters.sources.local import LocalFileSource
from trackforge.adapters.tagging.mutagen.reader import InitialTagsObserver
from trackforge.application.ports.acquisition import Acquirer
from trackforge.application.ports.execution import Capability
from trackforge.application.ports.repositories import UnitOfWork
from trackforge.application.ports.sources import SourceResolver
from trackforge.application.use_cases.acquire_item import AcquireItem
from trackforge.application.use_cases.observe_item import ObserveItem
from trackforge.application.use_cases.plan_item import PlanItem
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

    def source_resolvers(self) -> tuple[SourceResolver, ...]:
        """Return the source resolvers registered for this build."""

        return build_source_resolvers()

    def observer_capabilities(self) -> tuple[Capability, ...]:
        """Return the intrinsic observation capabilities registered for this build."""

        return build_observer_capabilities()


@dataclass(slots=True)
class CatalogContext:
    """An initialized catalog, its unit-of-work factory, and data-dir lock."""

    config: TrackForgeConfig
    catalog: Catalog
    lock: FileLock = field(init=False)
    cas: FileSystemCas = field(init=False)
    artifact_store: FileSystemArtifactStore = field(init=False)

    def __post_init__(self) -> None:
        self.lock = FileLock(self.config.data_dir)
        self.cas = build_cas(self.config)
        self.artifact_store = build_artifact_store(self.config)

    def unit_of_work(self) -> SqliteUnitOfWork:
        """Create a fresh short-lived unit of work."""

        return SqliteUnitOfWork(self.catalog)

    def source_resolvers(self) -> tuple[SourceResolver, ...]:
        """Resolvers available to plan and acquire local files."""

        return build_source_resolvers()

    def acquirers(self) -> tuple[Acquirer, ...]:
        """Acquirers wired to this catalog and its CAS."""

        return build_acquirers(self.cas, self.unit_of_work)

    def observer_capabilities(self) -> tuple[Capability, ...]:
        """Intrinsic observers registered for the observation pipeline."""

        return build_observer_capabilities()

    def plan_item(self) -> PlanItem:
        """Build the plan use case with this build's capabilities/resolvers."""

        return build_plan_item(
            capabilities=self.observer_capabilities(),
            resolvers=self.source_resolvers(),
        )

    def acquire_item(self) -> AcquireItem:
        """Build the acquisition use case."""

        return build_acquire_item()

    def observe_item(self) -> ObserveItem:
        """Build the observation use case over this catalog and artifact store."""

        return build_observe_item(
            self.config,
            self.unit_of_work,
            artifact_store=self.artifact_store,
            capabilities=self.observer_capabilities(),
        )

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


def build_source_resolvers() -> tuple[SourceResolver, ...]:
    """Return the source resolvers enabled in the local MVP."""

    return (LocalFileSource(),)


def build_acquirers(
    cas: FileSystemCas,
    unit_of_work_factory: Callable[[], UnitOfWork],
) -> tuple[Acquirer, ...]:
    """Return acquisition adapters bound to ``cas`` and the catalog."""

    return (LocalCopyAcquirer(cas, unit_of_work_factory),)


def build_observer_capabilities() -> tuple[Capability, ...]:
    """Return the intrinsic observation capabilities for the local MVP."""

    return (
        FfprobeProbe(),
        InitialTagsObserver(),
        PathStructureObserver(),
    )


def build_plan_item(
    *,
    capabilities: tuple[Capability, ...] | None = None,
    resolvers: tuple[SourceResolver, ...] | None = None,
) -> PlanItem:
    """Build the planning use case with explicit or default registrations."""

    return PlanItem(
        capabilities=capabilities if capabilities is not None else build_observer_capabilities(),
        resolvers=resolvers if resolvers is not None else build_source_resolvers(),
    )


def build_acquire_item() -> AcquireItem:
    """Build the acquisition use case."""

    return AcquireItem()


def build_observe_item(
    config: TrackForgeConfig,
    unit_of_work_factory: Callable[[], UnitOfWork],
    *,
    artifact_store: FileSystemArtifactStore | None = None,
    capabilities: tuple[Capability, ...] | None = None,
) -> ObserveItem:
    """Build the observation use case with the local MVP capability set."""

    return ObserveItem(
        unit_of_work_factory,
        capabilities if capabilities is not None else build_observer_capabilities(),
        artifact_store if artifact_store is not None else build_artifact_store(config),
        config={"offline": config.offline, "profile": config.profile},
    )


__all__ = [
    "AppContext",
    "CatalogContext",
    "build_acquirers",
    "build_acquire_item",
    "build_artifact_store",
    "build_cas",
    "build_catalog",
    "build_catalog_context",
    "build_context",
    "build_observer_capabilities",
    "build_observe_item",
    "build_plan_item",
    "build_source_resolvers",
    "build_unit_of_work",
]
