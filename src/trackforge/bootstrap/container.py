"""Composition root for TrackForge."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType

from trackforge.adapters.acquisition.local_copy import LocalCopyAcquirer
from trackforge.adapters.acquisition.ytdlp import YtDlpAcquirer
from trackforge.adapters.analysis.demucs import DemucsObserver
from trackforge.adapters.analysis.ffprobe import FfprobeProbe
from trackforge.adapters.analysis.llm import (
    OpenAICompatibleConfig,
    OpenAICompatibleProvider,
)
from trackforge.adapters.analysis.path_structure import PathStructureObserver
from trackforge.adapters.analysis.ranker import (
    CrossEncoderRanker,
    HybridRanker,
    LexicalRanker,
)
from trackforge.adapters.analysis.whisper import WhisperObserver
from trackforge.adapters.http.client import HttpClient
from trackforge.adapters.http.rate_limit import HostRateLimiter
from trackforge.adapters.metadata.observers import (
    AcoustIdObserver,
    ChromaprintObserver,
    CoverArtObserver,
    LrclibObserver,
    MusicBrainzObserver,
)
from trackforge.adapters.models.local import LocalModelManager
from trackforge.adapters.persistence.feedback import JsonlFeedbackJournal
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
from trackforge.application.ports.feedback import FeedbackJournal
from trackforge.application.ports.llm import LLMProvider
from trackforge.application.ports.models import ModelInfo, model_specs_from_config
from trackforge.application.ports.publishing import Publisher
from trackforge.application.ports.ranking import ClaimRanker
from trackforge.application.ports.rendering import Renderer, TagReader, TagWriter
from trackforge.application.ports.repositories import UnitOfWork
from trackforge.application.ports.sources import SourceResolver
from trackforge.application.services.llm_guardrails import LLMGuardrails
from trackforge.application.services.prompts import PromptLibrary
from trackforge.application.services.proxy_routing import ProxyRouter
from trackforge.application.use_cases.acquire_item import AcquireItem
from trackforge.application.use_cases.doctor import Doctor
from trackforge.application.use_cases.explain_item import ExplainItem
from trackforge.application.use_cases.export_feedback import ExportFeedback
from trackforge.application.use_cases.feedback_stats import FeedbackStats
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
        """Return the observation capabilities registered for this build."""

        return build_observer_capabilities(self.config, cas=self.cas)

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

        return build_observer_capabilities(self.config, cas=self.cas)

    def interpreter_capabilities(self) -> tuple[Interpreter, ...]:
        """Observation-to-claims interpreters registered for this build."""

        return build_interpreter_capabilities()

    def plan_item(self) -> PlanItem:
        """Build the plan use case with this build's capabilities/resolvers."""

        return build_plan_item(
            capabilities=self.observer_capabilities(),
            resolvers=self.source_resolvers(),
            config=self.config,
        )

    def acquire_item(self) -> AcquireItem:
        """Build the acquisition use case."""

        return build_acquire_item()

    def observe_item(self) -> ObserveItem:
        """Build the observation use case over this catalog and artifact store."""

        return build_observe_item(
            self.config,
            self.unit_of_work,
            cas=self.cas,
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
        """Build the policy/LLM resolution use case for this catalog."""

        return build_resolve_item(self.unit_of_work, config=self.config)

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

        return build_review_item(
            self.unit_of_work,
            journal=self.feedback_journal(),
            config=self.config,
        )

    def feedback_journal(self) -> JsonlFeedbackJournal:
        """Build the append-only feedback journal rooted at ``data_dir``."""

        return build_feedback_journal(self.config)

    def export_feedback(self) -> ExportFeedback:
        """Build the feedback export use case."""

        return build_export_feedback(
            self.config,
            self.unit_of_work,
            journal=self.feedback_journal(),
        )

    def feedback_stats(self) -> FeedbackStats:
        """Build the feedback statistics use case."""

        return build_feedback_stats(
            self.config,
            self.unit_of_work,
            journal=self.feedback_journal(),
        )

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
    return (
        LocalFileSource(),
        YtDlpSource(
            client=client,
            cookie_dir=cookie_dir,
            cookie_files=config.ytdlp_cookie_files if config is not None else None,
        ),
    )


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
                cookie_files=config.ytdlp_cookie_files if config is not None else None,
            )
        )
    return tuple(acquirers)


def _build_ytdlp_client(config: TrackForgeConfig | None = None) -> YtDlpClient:
    if config is None:
        return YtDlpClient()
    router = ProxyRouter.from_mapping(
        config.ytdlp_proxy or None,
        config.ytdlp_site_proxies,
    )
    return YtDlpClient(
        config.ytdlp_binary,
        timeout_metadata=config.ytdlp_timeout_metadata,
        timeout_download=config.ytdlp_timeout_download,
        cache_dir=config.cache_dir / "ytdlp",
        proxy_selector=router.proxy_for,
    )


def build_observer_capabilities(
    config: TrackForgeConfig | None = None,
    *,
    cas: FileSystemCas | None = None,
    http_client: HttpClient | None = None,
    fpcalc_executable: str = "fpcalc",
) -> tuple[Capability, ...]:
    """Return the observation capabilities for this build.

    Local observers are always registered.  Network metadata observers are
    opt-in: they are added only when the configuration has an AcoustID key,
    a MusicBrainz User-Agent, or an explicit ``metadata_enabled`` switch.
    """

    local: tuple[Capability, ...] = (
        FfprobeProbe(),
        InitialTagsObserver(),
        PathStructureObserver(),
        SourceMetadataObserver(),
    )
    if config is not None:
        # Registered even when unconfigured so `full` can report a visible
        # `model_missing` skip instead of silently omitting the ASR step.
        local = (*local, build_whisper_observer(config), build_demucs_observer(config))
    if config is None or not config.metadata_providers_enabled:
        return local
    client = http_client or HttpClient(
        contact=config.musicbrainz_user_agent or None,
        offline=config.offline,
        limiter=HostRateLimiter(),
        proxy=config.metadata_proxy or None,
    )
    return (
        *local,
        ChromaprintObserver(executable=fpcalc_executable),
        AcoustIdObserver(
            client=client,
            api_key=config.acoustid_api_key or None,
        ),
        MusicBrainzObserver(
            client=client,
            contact=config.musicbrainz_user_agent or None,
        ),
        CoverArtObserver(
            client=client,
            cas=cas,
            fetch_images=cas is not None,
        ),
        LrclibObserver(client=client),
    )


def build_demucs_observer(config: TrackForgeConfig) -> DemucsObserver:
    """Build the optional CPU Demucs observer from ``[models.stems]``."""

    return DemucsObserver(
        build_model_manager(config),
        config.stems_model,
        backend=config.stems_backend,
        segment=config.stems_segment,
        overlap=config.stems_overlap,
        jobs=config.stems_jobs,
        threads=config.stems_threads,
        memory_mb=config.stems_memory_mb,
    )


def build_whisper_observer(config: TrackForgeConfig) -> WhisperObserver:
    """Build the local Whisper observer from ``[models.asr]`` configuration."""

    return WhisperObserver(
        build_model_manager(config),
        config.asr_model,
        backend=config.asr_backend,
        compute_type=config.asr_compute_type,
        threads=config.asr_threads,
        vad_filter=config.asr_vad_filter,
        beam_size=config.asr_beam_size,
        memory_mb=config.asr_memory_mb,
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
    *,
    config: TrackForgeConfig | None = None,
    llm_provider: LLMProvider | None = None,
    guardrails: LLMGuardrails | None = None,
    prompts: PromptLibrary | None = None,
) -> ResolveItem:
    """Build the resolver and optional LLM provider for ``config``."""

    if config is None:
        return ResolveItem(
            unit_of_work_factory,
            llm_provider=llm_provider,
            guardrails=guardrails,
            prompts=prompts,
        )
    provider = llm_provider if llm_provider is not None else build_llm_provider(config)
    if config.offline or not config.send_text_to_cloud:
        provider = None
    disabled_reason = None
    if provider is None:
        if config.offline:
            disabled_reason = "offline mode blocks the LLM"
        elif not config.send_text_to_cloud:
            disabled_reason = "cloud text is disabled by privacy settings"
        else:
            disabled_reason = "no provider configured"
    library = prompts or PromptLibrary()
    return ResolveItem(
        unit_of_work_factory,
        llm_provider=provider,
        guardrails=guardrails
        or LLMGuardrails(
            max_cost_usd_per_item=config.llm_max_cost_usd_per_item,
        ),
        prompts=library,
        max_cost_usd_per_item=config.llm_max_cost_usd_per_item,
        llm_max_tokens=config.llm_max_tokens,
        llm_temperature=config.llm_temperature,
        llm_disabled_reason=disabled_reason,
    )


def build_llm_provider(config: TrackForgeConfig) -> OpenAICompatibleProvider | None:
    """Build the configured API provider; no default endpoint or model."""

    if config.offline or not config.send_text_to_cloud or not config.llm_configured:
        return None
    return OpenAICompatibleProvider(
        OpenAICompatibleConfig(
            base_url=config.llm_base_url,
            model=config.llm_model,
            api_key_env=config.llm_api_key_env,
            price_in=config.llm_price_in,
            price_out=config.llm_price_out,
            timeout_seconds=config.llm_timeout_seconds,
            max_retries=config.llm_max_retries,
            max_tokens=config.llm_max_tokens,
            temperature=config.llm_temperature,
            strict_schema=config.llm_strict_schema,
            thinking=config.llm_thinking,
            dotenv_path=str(Path(config.config_dir) / ".env"),
            proxy=config.llm_proxy or None,
        ),
    )


def build_model_manager(
    config: TrackForgeConfig,
    *,
    registered: tuple[ModelInfo, ...] = (),
    for_download: bool = False,
) -> LocalModelManager:
    """Build a model manager.

    Analysis never downloads weights.  ``for_download`` is reserved for the
    explicit ``models download`` command: only core ``--offline`` blocks it,
    not the offline-first ``[models] offline`` default.
    """

    manager = LocalModelManager(
        model_specs_from_config(config),
        models_dir=config.models_dir,
        offline=(config.offline if for_download else config.models_offline),
        max_heavy_models=config.models_max_heavy_models,
        proxy=config.models_proxy or None,
    )
    manager.add_registered(registered)
    return manager


def build_ranker(
    config: TrackForgeConfig,
    *,
    registered: tuple[ModelInfo, ...] = (),
) -> ClaimRanker:
    """Build the configured ranker; lexical remains the always-on baseline."""

    lexical = LexicalRanker()
    if config.ranker_strategy == "lexical":
        return lexical
    manager = build_model_manager(config, registered=registered)
    cross = CrossEncoderRanker(
        manager,
        config.ranker_model or None,
        tokenizer_path=(
            str(Path(config.ranker_model_path).expanduser().parent / "tokenizer.json")
            if config.ranker_model_path
            else None
        ),
        max_length=config.ranker_max_length,
        batch_size=config.ranker_batch_size,
        model_name=config.ranker_model,
    )
    return HybridRanker(lexical, cross)


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
        capabilities=(
            capabilities if capabilities is not None else build_observer_capabilities(config)
        ),
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
    *,
    journal: FeedbackJournal | None = None,
    config: TrackForgeConfig | None = None,
    resolve_item: ResolveItem | None = None,
    explain_item: ExplainItem | None = None,
) -> ReviewItem:
    """Build the review service over the shared resolver/explainer."""

    return ReviewItem(
        unit_of_work_factory,
        resolve_item
        if resolve_item is not None
        else build_resolve_item(
            unit_of_work_factory,
            config=config,
        ),
        explain_item if explain_item is not None else build_explain_item(unit_of_work_factory),
        journal=journal,
    )


def build_feedback_journal(
    config: TrackForgeConfig | Path | str,
) -> JsonlFeedbackJournal:
    """Build the JSONL journal rooted at ``data_dir/feedback``."""

    data_dir = config.data_dir if isinstance(config, TrackForgeConfig) else Path(config)
    return JsonlFeedbackJournal(data_dir)


def build_export_feedback(
    config: TrackForgeConfig,
    unit_of_work_factory: Callable[[], UnitOfWork],
    *,
    journal: FeedbackJournal | None = None,
) -> ExportFeedback:
    """Build the feedback export use case for ``config``."""

    return ExportFeedback(
        journal if journal is not None else build_feedback_journal(config),
        unit_of_work_factory,
        data_dir=config.data_dir,
    )


def build_feedback_stats(
    config: TrackForgeConfig,
    unit_of_work_factory: Callable[[], UnitOfWork],
    *,
    journal: FeedbackJournal | None = None,
) -> FeedbackStats:
    """Build the feedback statistics use case for ``config``."""

    return FeedbackStats(
        journal if journal is not None else build_feedback_journal(config),
        unit_of_work_factory,
        data_dir=config.data_dir,
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

    interpreter_capabilities = build_interpreter_capabilities()
    content_store = cas if cas is not None else build_cas(config)
    observer_capabilities = build_observer_capabilities(config, cas=content_store)
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
            config=_observe_config(config),
            metadata_enabled=config.metadata_providers_enabled or None,
        ),
        interpret_item=InterpretItem(
            unit_of_work_factory,
            interpreter_capabilities,
            artifact_store=artifacts,
        ),
        resolve_item=build_resolve_item(unit_of_work_factory, config=config),
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


def _observe_config(config: TrackForgeConfig) -> dict[str, object]:
    """Config subset that can influence observer cache keys."""

    return {
        "offline": config.offline,
        "profile": config.profile,
        "asr_model": config.asr_model,
        "asr_model_path": config.asr_model_path,
        "asr_model_version": config.asr_model_version,
        "asr_checksum": config.asr_checksum,
        "asr_license": config.asr_license,
        "asr_backend": config.asr_backend,
        "asr_compute_type": config.asr_compute_type,
        "asr_threads": config.asr_threads,
        "asr_vad_filter": config.asr_vad_filter,
        "asr_beam_size": config.asr_beam_size,
        "stems_model": config.stems_model,
        "stems_model_path": config.stems_model_path,
        "stems_model_version": config.stems_model_version,
        "stems_model_url": config.stems_model_url,
        "stems_model_file": config.stems_model_file,
        "stems_checksum": config.stems_checksum,
        "stems_license": config.stems_license,
        "stems_backend": config.stems_backend,
        "stems_segment": config.stems_segment,
        "stems_overlap": config.stems_overlap,
        "stems_jobs": config.stems_jobs,
        "stems_threads": config.stems_threads,
    }


def build_observe_item(
    config: TrackForgeConfig,
    unit_of_work_factory: Callable[[], UnitOfWork],
    *,
    cas: FileSystemCas | None = None,
    artifact_store: FileSystemArtifactStore | None = None,
    capabilities: tuple[Capability, ...] | None = None,
) -> ObserveItem:
    """Build the observation use case with the configured capability set."""

    content_store = cas if cas is not None else build_cas(config)
    return ObserveItem(
        unit_of_work_factory,
        capabilities
        if capabilities is not None
        else build_observer_capabilities(config, cas=content_store),
        artifact_store if artifact_store is not None else build_artifact_store(config),
        config=_observe_config(config),
        metadata_enabled=config.metadata_providers_enabled or None,
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
    "build_demucs_observer",
    "build_doctor",
    "build_explain_item",
    "build_export_feedback",
    "build_feedback_journal",
    "build_feedback_stats",
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
