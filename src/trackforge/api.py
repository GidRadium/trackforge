"""Public Python API for embedding TrackForge.

``AsyncTrackForge`` is the async-first facade used by the CLI and future HTTP
server.  ``TrackForge`` is a synchronous convenience wrapper that refuses to
run inside an already-running event loop, where it would deadlock.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from types import TracebackType
from typing import Any

from trackforge.application.dto.collections import (
    CollectionPlanOptions,
    CollectionPlanView,
    CollectionRunResult,
    parse_positions,
)
from trackforge.application.dto.decisions import ExplanationView
from trackforge.application.dto.feedback import (
    FeedbackExportResult,
    FeedbackStatsReport,
)
from trackforge.application.dto.plan import PlanOptions, PlanView
from trackforge.application.dto.progress import (
    CollectionProgressSnapshot,
    HandlerReport,
)
from trackforge.application.dto.results import (
    AnalysisView,
    DoctorReport,
    PublishResult,
    RenderView,
    RunOptions,
    RunResult,
    StatusView,
    render_view,
)
from trackforge.application.dto.views import (
    DecisionView,
    LibraryEntryView,
    ResolveView,
    ReviewView,
)
from trackforge.application.services.progress import ProgressBoard
from trackforge.application.use_cases.process_item import (
    ImportPath,
    ProcessItem,
    resolve_local_import_path,
)
from trackforge.bootstrap.container import (
    CatalogContext,
    build_catalog_context,
    build_collection_resolvers,
    build_doctor,
    build_plan_collection,
    build_plan_item,
    build_process_item,
)
from trackforge.config import TrackForgeConfig
from trackforge.domain.errors import (
    DomainValidationError,
    RecordNotFound,
    UnsupportedSource,
)
from trackforge.domain.model.collection import Collection
from trackforge.domain.model.ids import CollectionId, ItemId, RunId
from trackforge.domain.model.item import ItemStatus
from trackforge.domain.model.source import LocalPath, SourceRef, Url
from trackforge.pipeline.events import Event


class AsyncTrackForge:
    """Async-first facade over the application services."""

    def __init__(self, config: TrackForgeConfig | None = None) -> None:
        self._config = config if config is not None else TrackForgeConfig.from_file()
        self._context: CatalogContext | None = None
        self._process: ProcessItem | None = None
        self._importer: ImportPath | None = None
        self._feedback: _AsyncFeedbackAPI | None = None
        self._progress = ProgressBoard()
        self._closed = False

    @property
    def config(self) -> TrackForgeConfig:
        return self._config

    @property
    def progress_board(self) -> ProgressBoard:
        """In-memory live progress shared by runs started through this facade."""

        return self._progress

    @property
    def feedback(self) -> _AsyncFeedbackAPI:
        """Namespaced feedback API matching ``feedback.export/stats``."""

        if self._feedback is None:
            self._feedback = _AsyncFeedbackAPI(self)
        return self._feedback

    async def __aenter__(self) -> AsyncTrackForge:
        await self._ensure_open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def _ensure_open(self) -> CatalogContext:
        await self._ensure_not_closed()
        if self._context is None:
            context = build_catalog_context(self._config)
            await context.__aenter__()
            self._context = context
        return self._context

    async def _ensure_not_closed(self) -> None:
        if self._closed:
            raise DomainValidationError(
                "this AsyncTrackForge instance is closed",
                user_action="Create a new AsyncTrackForge instance.",
            )

    def _process_item(self) -> ProcessItem:
        if self._context is None:  # pragma: no cover - guarded by _ensure_open
            raise DomainValidationError("TrackForge is not open")
        if self._process is None:
            self._process = build_process_item(
                self._config,
                self._context.unit_of_work,
                cas=self._context.cas,
                artifact_store=self._context.artifact_store,
                progress=self._progress,
            )
        return self._process

    def _import_service(self) -> ImportPath:
        if self._importer is None:
            context = self._context
            if context is None:  # pragma: no cover - guarded by _ensure_open
                raise DomainValidationError("TrackForge is not open")
            process = self._process_item()
            self._importer = ImportPath(process, process.plan_item)
        return self._importer

    def _is_collection(self, source: SourceRef) -> bool:
        return any(
            resolver.supports_collection(source)
            for resolver in build_collection_resolvers(self._config)
        )

    async def plan(
        self,
        source: SourceRef | str | Path,
        options: RunOptions | PlanOptions | None = None,
    ) -> PlanView | CollectionPlanView:
        """Resolve a source and build a side-effect-free plan."""

        await self._ensure_not_closed()
        ref = _coerce_source(source)
        if self._is_collection(ref):
            offline = (
                options.offline
                if isinstance(options, (RunOptions, PlanOptions))
                else self._config.offline
            )
            return await self.collection_plan(
                ref,
                CollectionPlanOptions(
                    tag_basis=self._config.tag_basis,
                    offline=offline,
                ),
            )
        if isinstance(options, PlanOptions):
            plan_options = options
        else:
            opts = options or RunOptions()
            plan_options = PlanOptions(
                profile=opts.profile,
                enabled=opts.enabled if isinstance(opts.enabled, tuple) else (),
                disabled=opts.disabled if isinstance(opts.disabled, tuple) else (),
                offline=opts.offline,
            )
        return await build_plan_item(config=self._config).plan(ref, plan_options)

    async def collection_plan(
        self,
        source: SourceRef | str | Path,
        options: CollectionPlanOptions | None = None,
    ) -> CollectionPlanView:
        """Enumerate a playlist/folder and show the selected items, without writes."""

        await self._ensure_not_closed()
        ref = _coerce_source(source)
        opts = options or CollectionPlanOptions(
            tag_basis=self._config.tag_basis,
            offline=self._config.offline,
        )
        return await build_plan_collection(self._config).plan(ref, opts)

    async def collection_add(
        self,
        source: SourceRef | str | Path,
        options: RunOptions | None = None,
        *,
        collection_options: CollectionPlanOptions | None = None,
        resume: bool = False,
        fail_fast: bool = False,
        min_sleep: float | None = None,
    ) -> CollectionPlanView | CollectionRunResult:
        """Plan and sequentially process a playlist or folder collection."""

        opts = options or RunOptions()
        ref = _coerce_source(source)
        target_offline = opts.offline or self._config.offline
        plan_options = collection_options or CollectionPlanOptions(
            tag_basis=self._config.tag_basis,
            offline=target_offline,
        )
        if plan_options.offline != target_offline:
            plan_options = replace(plan_options, offline=target_offline)
        plan = await self.collection_plan(ref, plan_options)
        if opts.dry_run:
            return plan
        context = await self._ensure_open()
        return await context.process_collection(self._progress).process_plan(
            plan,
            opts,
            resume=resume,
            fail_fast=fail_fast,
            min_sleep=(self._config.collection_min_sleep if min_sleep is None else min_sleep),
            skip_positions=parse_positions(plan_options.skip),
        )

    async def collection_resume(
        self,
        collection_id: CollectionId | str,
        options: RunOptions | None = None,
        *,
        fail_fast: bool = False,
        min_sleep: float | None = None,
    ) -> CollectionRunResult:
        """Continue a persisted collection from its planned/acquired items."""

        context = await self._ensure_open()
        return await context.process_collection(self._progress).resume(
            CollectionId(str(collection_id)),
            options,
            fail_fast=fail_fast,
            min_sleep=(self._config.collection_min_sleep if min_sleep is None else min_sleep),
        )

    async def collection_progress(
        self,
        collection_id: CollectionId | str,
    ) -> CollectionProgressSnapshot:
        """Return the same latest-state snapshot a GUI/SSE consumer would read."""

        return self._progress.snapshot(str(collection_id))

    async def collection_handler_reports(
        self,
        collection_id: CollectionId | str,
    ) -> tuple[HandlerReport, ...]:
        """Return handler reports accumulated in memory for this run."""

        return self._progress.reports(str(collection_id))

    async def list_collections(self) -> tuple[Collection, ...]:
        """Return persisted collections, newest first."""

        context = await self._ensure_open()
        async with context.unit_of_work() as uow:
            return tuple(await uow.collections.list_all())

    async def get_collection(self, collection_id: CollectionId | str) -> Collection:
        """Return one persisted collection."""

        context = await self._ensure_open()
        target = CollectionId(str(collection_id))
        async with context.unit_of_work() as uow:
            collection = await uow.collections.get(target)
        if collection is None:
            raise RecordNotFound(
                "collection not found",
                context={"collection_id": target},
            )
        return collection

    async def run(
        self,
        plan_or_source: PlanView | SourceRef | str | Path,
        options: RunOptions | None = None,
    ) -> RunResult | PlanView | CollectionPlanView | CollectionRunResult:
        """Run a source, plan, or an existing item id."""

        opts = options or RunOptions()
        if isinstance(plan_or_source, PlanView):
            if opts.dry_run:
                return plan_or_source
            await self._ensure_open()
            return await self._process_item().run(plan_or_source.source, opts)
        if isinstance(plan_or_source, str) and _looks_like_item_id(plan_or_source):
            await self._ensure_open()
            return await self._process_item().run_item(ItemId(plan_or_source), opts)
        ref = _coerce_source(plan_or_source)
        if self._is_collection(ref):
            return await self.collection_add(ref, opts)
        if opts.dry_run:
            return await self.plan(ref, opts)
        await self._ensure_open()
        return await self._process_item().run(ref, opts)

    async def add(
        self,
        source: SourceRef | str | Path,
        options: RunOptions | None = None,
    ) -> RunResult | PlanView | CollectionPlanView | CollectionRunResult:
        """Plan and run one source (URL, file, folder, or playlist)."""

        opts = options or RunOptions()
        ref = _coerce_source(source)
        if self._is_collection(ref):
            return await self.collection_add(ref, opts)
        if opts.dry_run:
            return await self.plan(ref, opts)
        await self._ensure_open()
        if isinstance(ref, Url):
            return await self._process_item().run(ref, opts)
        if isinstance(ref, LocalPath):
            return await self.import_path(ref.path, opts)
        raise UnsupportedSource(
            "add supports URLs, local files, and collection folders",
            user_action="Pass an http(s) URL or a local audio path.",
            context={"source_type": type(ref).__name__},
        )

    async def import_path(
        self,
        path: str | Path,
        options: RunOptions | None = None,
    ) -> RunResult | PlanView:
        """Import a local file through the full local pipeline."""

        opts = options or RunOptions()
        if opts.dry_run:
            plan = await self.plan(resolve_local_import_path(path), opts)
            if isinstance(plan, PlanView):
                return plan
            raise UnsupportedSource(
                "a local file cannot resolve to a collection",
                context={"path": str(path)},
            )
        await self._ensure_open()
        return await self._import_service().import_path(path, opts)

    async def analyze(
        self,
        item_id: ItemId | str,
        options: RunOptions | None = None,
    ) -> AnalysisView:
        """Run observe + interpret for an acquired item."""

        context = await self._ensure_open()
        opts = options or RunOptions()
        target = ItemId(str(item_id))
        async with context.unit_of_work() as uow:
            item = await uow.items.get(target)
            if item is None:
                raise RecordNotFound("item not found", context={"item_id": target})
            status = item.status
        observations = None
        if status is ItemStatus.ACQUIRED:
            observations = await context.observe_item().observe(
                target,
                opts.profile,
                refresh=opts.refresh,
            )
        interpretation = None
        if status in {
            ItemStatus.OBSERVED,
            ItemStatus.ACQUIRED,
            ItemStatus.INTERPRETED,
            ItemStatus.RESOLVED,
            ItemStatus.RENDERED,
            ItemStatus.PUBLISHED,
        }:
            interpretation = await context.interpret_item().interpret(target)
        async with context.unit_of_work() as uow:
            updated = await uow.items.get(target)
        return AnalysisView(
            item_id=target,
            status=updated.status if updated is not None else status,
            observations=observations,
            interpretation=interpretation,
            warnings=(
                tuple(warning.message for warning in observations.warnings)
                if observations is not None
                else ()
            )
            + (tuple(interpretation.warnings) if interpretation is not None else ()),
        )

    async def resolve(
        self,
        item_id: ItemId | str,
        options: RunOptions | None = None,
    ) -> ResolveView:
        """Resolve an item and return per-field decision views."""

        context = await self._ensure_open()
        opts = options or RunOptions()
        target = ItemId(str(item_id))
        resolved = await context.resolve_item().resolve(target, opts.mode)
        async with context.unit_of_work() as uow:
            record = (
                await uow.decisions.get_record(resolved.decision_record_id)
                if resolved.decision_record_id is not None
                else None
            )
            claims = await uow.claims.list_by_item(target)
        by_id = {claim.id: claim for claim in claims}
        from trackforge.application.dto.decisions import AlternativeView, ClaimView

        decisions: list[DecisionView] = []
        if record is not None:
            for decision in record.decisions:
                alternatives = tuple(
                    AlternativeView(
                        claim=ClaimView.from_claim(by_id[claim_id]),
                        reason="not selected by the active policy",
                    )
                    for claim_id in decision.alternatives
                    if claim_id in by_id
                )
                decisions.append(
                    DecisionView(
                        field=decision.field,
                        value=decision.value,
                        confidence=decision.confidence,
                        rationale=decision.rationale,
                        mode=decision.mode,
                        decided_by=decision.decided_by,
                        locked=decision.locked,
                        alternatives=alternatives,
                    )
                )
        return ResolveView(
            item_id=resolved.item_id,
            decision_record_id=resolved.decision_record_id,
            decisions=tuple(decisions),
            fields=dict(resolved.fields),
            unknowns=resolved.unknowns,
            warnings=resolved.warnings,
        )

    async def render(
        self,
        item_id: ItemId | str,
        options: RunOptions | None = None,
    ) -> RenderView:
        """Render the current decisions into a verified bundle."""

        context = await self._ensure_open()
        opts = options or RunOptions()
        from trackforge.application.dto.rendering import RenderOptions

        result = await context.render_item().render(
            ItemId(str(item_id)),
            RenderOptions(target_format=opts.target_format),
        )
        return render_view(result)

    async def publish(
        self,
        item_id: ItemId | str,
        options: RunOptions | None = None,
    ) -> PublishResult:
        """Publish the latest rendered bundle to the managed library."""

        context = await self._ensure_open()
        opts = options or RunOptions()
        return await context.publish_item().publish(
            ItemId(str(item_id)),
            opts.publish,
            library_root=self._config.library_dir,
            naming=opts.naming,
            force="publish" in opts.force_steps,
        )

    async def review(
        self,
        item_id: ItemId | str,
        corrections: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
        *,
        action: str = "show",
        field: str | None = None,
        value: Any = None,
        lock: bool = False,
        reason: str | None = None,
        reason_code: str | None = None,
        reason_text: str | None = None,
        scope: str = "item",
        mode: str | None = None,
        auto: bool = False,
    ) -> ReviewView:
        """Review an item, optionally applying one or more corrections."""

        context = await self._ensure_open()
        review_service = context.review_item()
        target = ItemId(str(item_id))
        mode_name = mode or self._config.mode
        if corrections:
            if isinstance(corrections, Mapping):
                entries: list[Any] = [
                    {"field": key, "value": item} for key, item in corrections.items()
                ]
            else:
                entries = list(corrections)
            view = None
            for entry in entries:
                if isinstance(entry, Mapping):
                    entry_field = entry.get("field")
                    entry_value = entry.get("value")
                    entry_lock = bool(entry.get("lock", True))
                    entry_reason = entry.get("reason") or reason
                    entry_reason_code = entry.get("reason_code") or reason_code
                    entry_reason_text = entry.get("reason_text") or reason_text
                    entry_scope = entry.get("scope") or scope
                elif isinstance(entry, tuple) and len(entry) >= 2:
                    entry_field, entry_value = entry[0], entry[1]
                    entry_lock, entry_reason = True, reason
                    entry_reason_code, entry_reason_text = reason_code, reason_text
                    entry_scope = scope
                else:
                    entry_field = getattr(entry, "field", None)
                    entry_value = getattr(entry, "value", None)
                    entry_lock = bool(getattr(entry, "lock", True))
                    entry_reason = getattr(entry, "reason", None) or reason
                    entry_reason_code = getattr(entry, "reason_code", None) or reason_code
                    entry_reason_text = getattr(entry, "reason_text", None) or reason_text
                    entry_scope = getattr(entry, "scope", scope)
                if not isinstance(entry_field, str):
                    raise DomainValidationError("correction requires a field")
                view = await review_service.review(
                    target,
                    action="edit",
                    field=entry_field,
                    value=entry_value,
                    lock=entry_lock,
                    reason=entry_reason,
                    reason_code=entry_reason_code,
                    reason_text=entry_reason_text,
                    scope=entry_scope,
                    mode=mode_name,
                )
            if view is None:
                raise DomainValidationError("no correction was applied")
            return view
        return await review_service.review(
            target,
            action=action,
            field=field,
            value=value,
            lock=lock,
            reason=reason,
            reason_code=reason_code,
            reason_text=reason_text,
            scope=scope,
            mode=mode_name,
            auto=auto,
        )

    async def explain(self, item_id: ItemId | str, field: str) -> ExplanationView:
        """Explain one field decision."""

        context = await self._ensure_open()
        return await context.explain_item().explain(ItemId(str(item_id)), field)

    async def feedback_export(
        self,
        *,
        format: str = "jsonl",
        output: Path | str | None = None,
        item_id: ItemId | str | None = None,
        field: str | None = None,
        since: Any = None,
    ) -> FeedbackExportResult:
        """Export redacted feedback without sending anything to the network."""

        context = await self._ensure_open()
        return await context.export_feedback().export(
            format=format,
            output=output,
            item_id=item_id,
            field=field,
            since=since,
        )

    async def feedback_stats(
        self,
        *,
        group_by: str = "field",
        item_id: ItemId | str | None = None,
        field: str | None = None,
        since: Any = None,
    ) -> FeedbackStatsReport:
        """Return correction/review/unknown statistics without claiming accuracy."""

        context = await self._ensure_open()
        return await context.feedback_stats().calculate(
            group_by=group_by,
            item_id=item_id,
            field=field,
            since=since,
        )

    async def doctor(self) -> DoctorReport:
        """Check the environment without requiring an open catalog."""

        return await build_doctor(self._config).run()

    async def status(self, run_id: RunId | str) -> StatusView:
        """Return persisted run/step status."""

        await self._ensure_open()
        return await self._process_item().status(RunId(str(run_id)))

    async def resume(
        self,
        run_id: RunId | str,
        options: RunOptions | None = None,
    ) -> RunResult:
        """Continue a failed or paused run from persisted state."""

        await self._ensure_open()
        return await self._process_item().resume(RunId(str(run_id)), options)

    async def events(self, run_id: RunId | str) -> AsyncIterator[Event]:
        """Yield in-process events for ``run_id`` (including replay)."""

        if self._process is None:
            return
        async for event in self._process.events.events(RunId(str(run_id))):
            yield event

    async def cancel(self, run_id: RunId | str) -> bool:
        """Request cooperative cancellation of an in-process run."""

        if self._process is None:
            return False
        return self._process.cancel(RunId(str(run_id)))

    async def close(self) -> None:
        """Release the data-directory lock and catalog connections."""

        if self._context is not None:
            await self._context.__aexit__(None, None, None)
            self._context = None
        self._process = None
        self._importer = None
        self._closed = True


class _AsyncFeedbackAPI:
    """Small namespace object exposed as ``AsyncTrackForge.feedback``."""

    def __init__(self, facade: AsyncTrackForge) -> None:
        self._facade = facade

    async def export(self, **kwargs: Any) -> FeedbackExportResult:
        return await self._facade.feedback_export(**kwargs)

    async def stats(self, **kwargs: Any) -> FeedbackStatsReport:
        return await self._facade.feedback_stats(**kwargs)


class _SyncFeedbackAPI:
    """Small namespace object exposed as ``TrackForge.feedback``."""

    def __init__(self, facade: TrackForge) -> None:
        self._facade = facade

    def export(self, **kwargs: Any) -> FeedbackExportResult:
        return self._facade.feedback_export(**kwargs)

    def stats(self, **kwargs: Any) -> FeedbackStatsReport:
        return self._facade.feedback_stats(**kwargs)


class TrackForge:
    """Synchronous facade; use :class:`AsyncTrackForge` inside an event loop."""

    def __init__(self, config: TrackForgeConfig | None = None) -> None:
        self._async = AsyncTrackForge(config)
        self._entered = False
        self._feedback: _SyncFeedbackAPI | None = None

    @property
    def feedback(self) -> _SyncFeedbackAPI:
        if self._feedback is None:
            self._feedback = _SyncFeedbackAPI(self)
        return self._feedback

    @property
    def config(self) -> TrackForgeConfig:
        return self._async.config

    @property
    def progress_board(self) -> ProgressBoard:
        return self._async.progress_board

    def _call[T](
        self,
        function: Callable[..., Coroutine[Any, Any, T]],
        *args: Any,
        **kwargs: Any,
    ) -> T:
        _reject_running_loop()
        return asyncio.run(function(*args, **kwargs))

    def __enter__(self) -> TrackForge:
        _reject_running_loop()
        asyncio.run(self._async.__aenter__())
        self._entered = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        _reject_running_loop()
        asyncio.run(self._async.close())
        self._entered = False

    def plan(
        self,
        source: SourceRef | str | Path,
        options: RunOptions | PlanOptions | None = None,
    ) -> PlanView | CollectionPlanView:
        return self._call(self._async.plan, source, options)

    def collection_plan(
        self,
        source: SourceRef | str | Path,
        options: CollectionPlanOptions | None = None,
    ) -> CollectionPlanView:
        return self._call(self._async.collection_plan, source, options)

    def collection_add(
        self,
        source: SourceRef | str | Path,
        options: RunOptions | None = None,
        *,
        collection_options: CollectionPlanOptions | None = None,
        resume: bool = False,
        fail_fast: bool = False,
        min_sleep: float | None = None,
    ) -> CollectionPlanView | CollectionRunResult:
        return self._call(
            self._async.collection_add,
            source,
            options,
            collection_options=collection_options,
            resume=resume,
            fail_fast=fail_fast,
            min_sleep=min_sleep,
        )

    def collection_resume(
        self,
        collection_id: CollectionId | str,
        options: RunOptions | None = None,
        *,
        fail_fast: bool = False,
        min_sleep: float | None = None,
    ) -> CollectionRunResult:
        return self._call(
            self._async.collection_resume,
            collection_id,
            options,
            fail_fast=fail_fast,
            min_sleep=min_sleep,
        )

    def collection_progress(
        self,
        collection_id: CollectionId | str,
    ) -> CollectionProgressSnapshot:
        return self._call(
            self._async.collection_progress,
            collection_id,
        )

    def collection_handler_reports(
        self,
        collection_id: CollectionId | str,
    ) -> tuple[HandlerReport, ...]:
        return self._call(
            self._async.collection_handler_reports,
            collection_id,
        )

    def list_collections(self) -> tuple[Collection, ...]:
        return self._call(self._async.list_collections)

    def get_collection(self, collection_id: CollectionId | str) -> Collection:
        return self._call(self._async.get_collection, collection_id)

    def run(
        self,
        plan_or_source: PlanView | SourceRef | str | Path,
        options: RunOptions | None = None,
    ) -> RunResult | PlanView | CollectionPlanView | CollectionRunResult:
        return self._call(self._async.run, plan_or_source, options)

    def add(
        self,
        source: SourceRef | str | Path,
        options: RunOptions | None = None,
    ) -> RunResult | PlanView | CollectionPlanView | CollectionRunResult:
        return self._call(self._async.add, source, options)

    def import_path(
        self,
        path: str | Path,
        options: RunOptions | None = None,
    ) -> RunResult | PlanView:
        return self._call(self._async.import_path, path, options)

    def analyze(
        self,
        item_id: ItemId | str,
        options: RunOptions | None = None,
    ) -> AnalysisView:
        return self._call(self._async.analyze, item_id, options)

    def resolve(
        self,
        item_id: ItemId | str,
        options: RunOptions | None = None,
    ) -> ResolveView:
        return self._call(self._async.resolve, item_id, options)

    def render(
        self,
        item_id: ItemId | str,
        options: RunOptions | None = None,
    ) -> RenderView:
        return self._call(self._async.render, item_id, options)

    def publish(
        self,
        item_id: ItemId | str,
        options: RunOptions | None = None,
    ) -> PublishResult:
        return self._call(self._async.publish, item_id, options)

    def review(self, item_id: ItemId | str, *args: Any, **kwargs: Any) -> ReviewView:
        return self._call(self._async.review, item_id, *args, **kwargs)

    def explain(self, item_id: ItemId | str, field: str) -> ExplanationView:
        return self._call(self._async.explain, item_id, field)

    def feedback_export(self, **kwargs: Any) -> FeedbackExportResult:
        return self._call(self._async.feedback_export, **kwargs)

    def feedback_stats(self, **kwargs: Any) -> FeedbackStatsReport:
        return self._call(self._async.feedback_stats, **kwargs)

    def doctor(self) -> DoctorReport:
        return self._call(self._async.doctor)

    def status(self, run_id: RunId | str) -> StatusView:
        return self._call(self._async.status, run_id)

    def resume(
        self,
        run_id: RunId | str,
        options: RunOptions | None = None,
    ) -> RunResult:
        return self._call(self._async.resume, run_id, options)

    def cancel(self, run_id: RunId | str) -> bool:
        return self._call(self._async.cancel, run_id)

    def events(self, run_id: RunId | str) -> AsyncIterator[Event]:
        return self._async.events(run_id)

    def close(self) -> None:
        _reject_running_loop()
        asyncio.run(self._async.close())
        self._entered = False

    def __repr__(self) -> str:
        return f"TrackForge(config={self.config!r})"


def _reject_running_loop() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError(
        "TrackForge is synchronous and cannot run inside an active event loop; "
        "use AsyncTrackForge instead."
    )


def _coerce_source(value: SourceRef | str | Path) -> SourceRef:
    if isinstance(value, (LocalPath, Url)):
        return value
    if isinstance(value, Path):
        return LocalPath(str(value))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise UnsupportedSource("source must not be empty")
        if text.startswith(("http://", "https://")):
            return Url(text)
        return LocalPath(text)
    # TextQuery/CollectionRef are valid SourceRef variants from the domain.
    from trackforge.domain.model.source import CollectionRef, TextQuery

    if isinstance(value, (TextQuery, CollectionRef)):
        return value
    raise DomainValidationError(
        "source must be a path, URL, or SourceRef",
        context={"actual_type": type(value).__name__},
    )


def _looks_like_item_id(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError):
        return False
    return True


__all__ = [
    "AsyncTrackForge",
    "CollectionPlanOptions",
    "CollectionPlanView",
    "CollectionRunResult",
    "FeedbackExportResult",
    "FeedbackStatsReport",
    "LibraryEntryView",
    "RunOptions",
    "TrackForge",
]
