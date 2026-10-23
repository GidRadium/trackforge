"""Composition root for TrackForge."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from types import TracebackType

from trackforge.adapters.acquisition.local_copy import LocalCopyAcquirer
from trackforge.adapters.acquisition.ytdlp import YtDlpAcquirer
from trackforge.adapters.analysis.ffprobe import FfprobeProbe
from trackforge.adapters.analysis.path_structure import PathStructureObserver
from trackforge.adapters.persistence.filesystem.artifacts import FileSystemArtifactStore
from trackforge.adapters.persistence.filesystem.cas import FileSystemCas
from trackforge.adapters.persistence.sqlite.engine import Catalog, initialize_catalog
from trackforge.adapters.persistence.sqlite.file_lock import FileLock
from trackforge.adapters.persistence.sqlite.unit_of_work import SqliteUnitOfWork
from trackforge.adapters.publishing.filesystem import FilesystemPublisher
from trackforge.adapters.rendering.ffmpeg import FfmpegRenderer
from trackforge.adapters.sources.local import LocalFileSource
from trackforge.adapters.sources.ytdlp.client import YtDlpClient
from trackforge.adapters.sources.ytdlp.metadata import SourceMetadataObserver
from trackforge.adapters.sources.ytdlp.source import YtDlpSource
from trackforge.adapters.tagging.mutagen.reader import InitialTagsObserver, MutagenTagReader
from trackforge.adapters.tagging.mutagen.writer import MutagenTagWriter
from trackforge.application.ports.acquisition import Acquirer
from trackforge.application.ports.execution import Capability
from trackforge.application.ports.publishing import Publisher
from trackforge.application.ports.rendering import Renderer, TagReader, TagWriter
from trackforge.application.ports.repositories import UnitOfWork
from trackforge.application.ports.sources import SourceResolver
from trackforge.application.use_cases.acquire_item import AcquireItem
from trackforge.application.use_cases.doctor import Doctor
from trackforge.application.use_cases.explain_item import ExplainItem
from trackforge.application.use_cases.import_path import ImportPath
from trackforge.application.use_cases.interpret_item import (
    DEFAULT_INTERPRETERS,
    Interpreter,
    InterpretItem,
)
from trackforge.application.use_cases.observe_item import ObserveItem
from trackforge.application.use_cases.plan_item import PlanItem
from trackforge.application.use_cases.process_item import ProcessItem
from trackforge.application.use_cases.publish_item import PublishItem
from trackforge.application.use_cases.render_item import RenderItem
from trackforge.application.use_cases.resolve_item import ResolveItem
from trackforge.application.use_cases.review_item import ReviewItem
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

        return build_source_resolvers(self.config)

    def observer_capabilities(self) -> tuple[Capability, ...]:
        """Return the intrinsic observation capabilities registered for this build."""

        return build_observer_capabilities()

    def interpreter_capabilities(self) -> tuple[Interpreter, ...]:
        """Return observation-to-claims interpreters for this build."""

        return build_interpreter_capabilities()


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
        """Resolvers available to plan and acquire local files and URLs."""

        return build_source_resolvers(self.config)

    def acquirers(self) -> tuple[Acquirer, ...]:
        """Acquirers wired to this catalog and its CAS."""

        return build_acquirers(
            self.cas,
            self.unit_of_work,
            config=self.config,
            artifact_store=self.artifact_store,
        )

    def observer_capabilities(self) -> tuple[Capability, ...]:
        """Intrinsic observers registered for the observation pipeline."""

        return build_observer_capabilities()

    def interpreter_capabilities(self) -> tuple[Interpreter, ...]:
        """Observation-to-claims interpreters registered for this build."""

        return build_interpreter_capabilities()

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

    def interpret_item(self) -> InterpretItem:
        """Build the interpretation use case with default interpreters."""

        return build_interpret_item(
            self.unit_of_work,
            artifact_store=self.artifact_store,
            interpreters=self.interpreter_capabilities(),
        )

    def resolve_item(self) -> ResolveItem:
        """Build the policy resolution use case for this catalog."""

        return build_resolve_item(self.unit_of_work)

    def explain_item(self) -> ExplainItem:
        """Build the explanation use case for this catalog."""

        return build_explain_item(self.unit_of_work)

    def render_item(self) -> RenderItem:
        """Build the render/tag/verify use case for this catalog."""

        return build_render_item(self.config, self.unit_of_work)

    def publish_item(self) -> PublishItem:
        """Build the managed-library publisher for this catalog."""

        return build_publish_item(self.config, self.unit_of_work)

    def process_item(self) -> ProcessItem:
        """Build the end-to-end local pipeline for this catalog."""

        return build_process_item(
            self.config,
            self.unit_of_work,
            cas=self.cas,
            artifact_store=self.artifact_store,
        )

    def import_path(self) -> ImportPath:
        """Build the local import use case."""

        return build_import_path(
            self.config,
            self.unit_of_work,
            cas=self.cas,
            artifact_store=self.artifact_store,
        )

    def review_item(self) -> ReviewItem:
        """Build the review/feedback use case."""

        return build_review_item(self.unit_of_work)

    def doctor(self) -> Doctor:
        """Build the environment doctor for this configuration."""

        return build_doctor(self.config)

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


def build_source_resolvers(
    config: TrackForgeConfig | None = None,
) -> tuple[SourceResolver, ...]:
    """Return the source resolvers enabled in this build."""

    cookie_dir = config.cookies_dir if config is not None else None
    client = _build_ytdlp_client(config)
    return (LocalFileSource(), YtDlpSource(client=client, cookie_dir=cookie_dir))


def build_acquirers(
    cas: FileSystemCas,
    unit_of_work_factory: Callable[[], UnitOfWork],
    *,
    config: TrackForgeConfig | None = None,
    artifact_store: FileSystemArtifactStore | None = None,
) -> tuple[Acquirer, ...]:
    """Return acquisition adapters bound to ``cas`` and the catalog."""

    acquirers: list[Acquirer] = [LocalCopyAcquirer(cas, unit_of_work_factory)]
    if config is not None or artifact_store is not None:
        acquirers.append(
            YtDlpAcquirer(
                cas,
                unit_of_work_factory,
                artifact_store=artifact_store,
                cache_dir=config.cache_dir if config is not None else None,
                client=_build_ytdlp_client(config),
                cookie_dir=config.cookies_dir if config is not None else None,
            )
        )
    return tuple(acquirers)


def _build_ytdlp_client(config: TrackForgeConfig | None = None) -> YtDlpClient:
    if config is None:
        return YtDlpClient()
    return YtDlpClient(
        config.ytdlp_binary,
        timeout_metadata=config.ytdlp_timeout_metadata,
        timeout_download=config.ytdlp_timeout_download,
        cache_dir=config.cache_dir / "ytdlp",
    )


def build_observer_capabilities() -> tuple[Capability, ...]:
    """Return the intrinsic observation capabilities for this build."""

    return (
        FfprobeProbe(),
        InitialTagsObserver(),
        PathStructureObserver(),
        SourceMetadataObserver(),
    )


def build_interpreter_capabilities() -> tuple[Interpreter, ...]:
    """Return the pure observation-to-claims interpreters."""

    return DEFAULT_INTERPRETERS


def build_interpret_item(
    unit_of_work_factory: Callable[[], UnitOfWork],
    *,
    artifact_store: FileSystemArtifactStore | None = None,
    interpreters: tuple[Interpreter, ...] | None = None,
) -> InterpretItem:
    """Build the interpretation use case with explicit or default interpreters."""

    return InterpretItem(
        unit_of_work_factory,
        interpreters if interpreters is not None else build_interpreter_capabilities(),
        artifact_store=artifact_store,
    )


def build_resolve_item(
    unit_of_work_factory: Callable[[], UnitOfWork],
) -> ResolveItem:
    """Build the policy resolver over this catalog."""

    return ResolveItem(unit_of_work_factory)


def build_explain_item(
    unit_of_work_factory: Callable[[], UnitOfWork],
) -> ExplainItem:
    """Build the explanation use case over this catalog."""

    return ExplainItem(unit_of_work_factory)


def build_render_item(
    config: TrackForgeConfig,
    unit_of_work_factory: Callable[[], UnitOfWork],
    *,
    renderer: Renderer | None = None,
    tag_writer: TagWriter | None = None,
    tag_reader: TagReader | None = None,
) -> RenderItem:
    """Build the renderer and mutagen adapters for this catalog."""

    reader: TagReader = tag_reader if tag_reader is not None else MutagenTagReader()
    writer: TagWriter = (
        tag_writer
        if tag_writer is not None
        else MutagenTagWriter(reader=reader if isinstance(reader, MutagenTagReader) else None)
    )
    cover_loader = reader.read_cover if isinstance(reader, MutagenTagReader) else None
    return RenderItem(
        unit_of_work_factory,
        renderer if renderer is not None else FfmpegRenderer(),
        writer,
        reader,
        config.data_dir,
        cover_loader=cover_loader,
    )


def build_plan_item(
    *,
    capabilities: tuple[Capability, ...] | None = None,
    resolvers: tuple[SourceResolver, ...] | None = None,
    config: TrackForgeConfig | None = None,
) -> PlanItem:
    """Build the planning use case with explicit or default registrations."""

    return PlanItem(
        capabilities=capabilities if capabilities is not None else build_observer_capabilities(),
        resolvers=(resolvers if resolvers is not None else build_source_resolvers(config)),
    )


def build_publisher(config: TrackForgeConfig) -> Publisher:
    """Build the managed-library publisher for ``config``."""

    return FilesystemPublisher(workspace_root=config.data_dir)


def build_publish_item(
    config: TrackForgeConfig,
    unit_of_work_factory: Callable[[], UnitOfWork],
    *,
    publisher: Publisher | None = None,
) -> PublishItem:
    """Build the publish use case rooted at the configured library."""

    return PublishItem(
        unit_of_work_factory,
        publisher if publisher is not None else build_publisher(config),
        workspace_root=config.data_dir,
        library_root=config.library_dir,
    )


def build_review_item(
    unit_of_work_factory: Callable[[], UnitOfWork],
) -> ReviewItem:
    """Build the review service over the shared resolver/explainer."""

    return ReviewItem(
        unit_of_work_factory,
        build_resolve_item(unit_of_work_factory),
        build_explain_item(unit_of_work_factory),
    )


def build_doctor(config: TrackForgeConfig) -> Doctor:
    """Build the doctor checks for ``config`` without opening the catalog."""

    return Doctor(config)


def build_process_item(
    config: TrackForgeConfig,
    unit_of_work_factory: Callable[[], UnitOfWork],
    *,
    cas: FileSystemCas | None = None,
    artifact_store: FileSystemArtifactStore | None = None,
    publisher: Publisher | None = None,
) -> ProcessItem:
    """Build the full local pipeline with one consistent capability set."""

    observer_capabilities = build_observer_capabilities()
    interpreter_capabilities = build_interpreter_capabilities()
    content_store = cas if cas is not None else build_cas(config)
    artifacts = artifact_store if artifact_store is not None else build_artifact_store(config)
    plan = PlanItem(
        capabilities=observer_capabilities,
        resolvers=build_source_resolvers(config),
    )
    return ProcessItem(
        unit_of_work_factory,
        plan_item=plan,
        acquire_item=build_acquire_item(),
        acquirers=build_acquirers(
            content_store,
            unit_of_work_factory,
            config=config,
            artifact_store=artifacts,
        ),
        observe_item=ObserveItem(
            unit_of_work_factory,
            observer_capabilities,
            artifacts,
            config={"offline": config.offline, "profile": config.profile},
        ),
        interpret_item=InterpretItem(
            unit_of_work_factory,
            interpreter_capabilities,
            artifact_store=artifacts,
        ),
        resolve_item=build_resolve_item(unit_of_work_factory),
        render_item=build_render_item(config, unit_of_work_factory),
        publish_item=build_publish_item(
            config,
            unit_of_work_factory,
            publisher=publisher,
        ),
        library_root=config.library_dir,
    )


def build_import_path(
    config: TrackForgeConfig,
    unit_of_work_factory: Callable[[], UnitOfWork],
    *,
    cas: FileSystemCas | None = None,
    artifact_store: FileSystemArtifactStore | None = None,
    publisher: Publisher | None = None,
) -> ImportPath:
    """Build the import use case, sharing its process item with the caller."""

    process = build_process_item(
        config,
        unit_of_work_factory,
        cas=cas,
        artifact_store=artifact_store,
        publisher=publisher,
    )
    return ImportPath(process, process.plan_item)


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
    "build_doctor",
    "build_explain_item",
    "build_import_path",
    "build_interpret_item",
    "build_interpreter_capabilities",
    "build_observer_capabilities",
    "build_observe_item",
    "build_plan_item",
    "build_process_item",
    "build_publish_item",
    "build_publisher",
    "build_render_item",
    "build_resolve_item",
    "build_review_item",
    "build_source_resolvers",
    "build_unit_of_work",
]
