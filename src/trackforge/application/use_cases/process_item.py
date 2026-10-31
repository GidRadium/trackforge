"""End-to-end local processing: plan → acquire → observe → … → publish.

``ProcessItem`` is the only application service that sequences the individual
use cases.  It owns run/step records, cancellation, cache reuse, and the
decision to publish an existing bundle instead of rendering it again.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, TypeVar, cast

from trackforge.application.dto.collections import CollectionItemContext
from trackforge.application.dto.db import (
    RunItemStatus,
    RunStatus,
    StepRecord,
    StepStatus,
)
from trackforge.application.dto.plan import AcquireOptions, PlanOptions, PlanView
from trackforge.application.dto.progress import (
    HandlerReport,
    HandlerStatus,
    ProgressContext,
    ProgressStatus,
)
from trackforge.application.dto.rendering import RenderOptions
from trackforge.application.dto.results import (
    RunItemView,
    RunOptions,
    RunResult,
    RunStepView,
    StatusView,
)
from trackforge.application.ports.acquisition import Acquirer
from trackforge.application.ports.repositories import UnitOfWork
from trackforge.application.ports.sources import absolute_local_path
from trackforge.application.services.progress import ProgressBoard, ProgressTracker
from trackforge.application.services.sidecar import SidecarCollection
from trackforge.application.services.tag_basis import (
    TagBasisApplication,
    TagBasisService,
)
from trackforge.application.use_cases.acquire_item import AcquireItem
from trackforge.application.use_cases.interpret_item import InterpretItem
from trackforge.application.use_cases.observe_item import ObserveItem
from trackforge.application.use_cases.plan_item import PlanItem
from trackforge.application.use_cases.publish_item import PublishItem
from trackforge.application.use_cases.render_item import RenderItem
from trackforge.application.use_cases.resolve_item import ResolveItem
from trackforge.domain.errors import (
    CandidateNotFound,
    DrmProtected,
    RecordNotFound,
    RenderFailed,
    TrackForgeError,
    UnsupportedSource,
)
from trackforge.domain.model._validation import utc_now
from trackforge.domain.model.claims import Subject
from trackforge.domain.model.ids import ItemId, RunId
from trackforge.domain.model.item import Item, ItemStatus
from trackforge.domain.model.media import FileRole, IntakeStatus
from trackforge.domain.model.outputs import OutputBundle, Render
from trackforge.domain.model.source import Candidate, LocalPath, SourceRef, Url
from trackforge.pipeline.events import Event, EventBus, EventType

_STEP_CAPABILITIES = (
    "plan",
    "acquire",
    "observe",
    "interpret",
    "resolve",
    "render",
    "publish",
)
_STEP_VERSIONS = {name: "1" for name in _STEP_CAPABILITIES}
T = TypeVar("T")


class _RunItemsReader(Protocol):
    async def list_items(self, run_id: RunId) -> list[Any]: ...


class _BundleReader(Protocol):
    async def find_bundle_by_render(self, render_id: str) -> OutputBundle | None: ...


class ProcessItem:
    """Run the local pipeline and persist observable run/step state."""

    def __init__(
        self,
        unit_of_work_factory: Callable[[], UnitOfWork],
        *,
        plan_item: PlanItem,
        acquire_item: AcquireItem,
        acquirers: Sequence[Acquirer],
        observe_item: ObserveItem,
        interpret_item: InterpretItem,
        resolve_item: ResolveItem,
        render_item: RenderItem,
        publish_item: PublishItem,
        library_root: Path,
        events: EventBus | None = None,
        tag_basis_service: TagBasisService | None = None,
        progress: ProgressBoard | None = None,
    ) -> None:
        self._uow_factory = unit_of_work_factory
        self._plan_item = plan_item
        self._acquire_item = acquire_item
        self._acquirers = tuple(acquirers)
        self._observe_item = observe_item
        self._interpret_item = interpret_item
        self._resolve_item = resolve_item
        self._render_item = render_item
        self._publish_item = publish_item
        self._library_root = Path(library_root)
        self._events = events or EventBus()
        self._tag_basis = tag_basis_service or TagBasisService(resolve_item)
        self._progress = progress if progress is not None else ProgressBoard()
        self._cancel_events: dict[str, asyncio.Event] = {}

    @property
    def events(self) -> EventBus:
        return self._events

    @property
    def plan_item(self) -> PlanItem:
        return self._plan_item

    @property
    def progress_board(self) -> ProgressBoard:
        return self._progress

    async def plan(self, source: SourceRef, options: RunOptions | None = None) -> PlanView:
        """Build the side-effect-free plan for ``source``."""

        opts = options or RunOptions()
        return await self._plan_item.plan(
            source,
            PlanOptions(
                profile=opts.profile,
                enabled=opts.enabled if isinstance(opts.enabled, tuple) else (),
                disabled=opts.disabled if isinstance(opts.disabled, tuple) else (),
                offline=opts.offline,
            ),
        )

    async def run(
        self,
        source: SourceRef,
        options: RunOptions | None = None,
    ) -> RunResult | PlanView:
        """Execute the full pipeline, or return a plan in dry-run mode."""

        opts = options or RunOptions()
        plan = await self.plan(source, opts)
        if opts.dry_run:
            return plan
        if plan.blocked_reason is not None or not plan.candidates:
            raise _plan_failure(plan)

        async with self._uow_factory() as uow:
            run = await uow.runs.create_run(opts.profile, opts.mode, status=RunStatus.PLANNED)
        cancel = asyncio.Event()
        self._cancel_events[str(run.id)] = cancel
        label = _plan_label(plan, source)
        tracker = self._single_tracker(run.id, label)
        reports: list[HandlerReport] = []
        self._publish_event(EventType.RUN_STARTED, run.id, status=RunStatus.PLANNED.value)
        try:
            await self._set_run_status(run.id, RunStatus.RUNNING, started=True)
            tracker.stage("plan", message="planned")
            reports.append(
                self._finish_stage(
                    tracker,
                    "plan",
                    utc_now(),
                    time.monotonic(),
                    summary=f"{len(plan.candidates)} candidate(s)",
                )
            )
            tracker.stage("acquire", message="starting download")
            acquire_started_at = utc_now()
            acquire_started = time.monotonic()
            try:
                item_id, acquire_reused = await self._acquire_for_run(
                    run.id,
                    source,
                    plan,
                    opts,
                    progress=tracker.percent,
                )
            except BaseException:
                tracker.failed("acquisition failed")
                raise
            if item_id is None:  # pragma: no cover - defensive
                raise UnsupportedSource("source could not be acquired")
            tracker.bind(item_id=item_id)
            acquire_summary, acquire_details = await self._acquire_summary(
                item_id,
                reused=acquire_reused,
            )
            reports.append(
                self._finish_stage(
                    tracker,
                    "acquire",
                    acquire_started_at,
                    acquire_started,
                    summary=acquire_summary,
                    details=acquire_details,
                    status=(HandlerStatus.CACHED if acquire_reused else HandlerStatus.SUCCEEDED),
                    cache_hit=acquire_reused,
                )
            )
            await self._set_run_item(run.id, item_id, RunItemStatus.RUNNING)
            result = await self._continue_item(
                run.id,
                item_id,
                opts,
                acquire_cached=acquire_reused,
                cancel=cancel,
                tracker=tracker,
                handler_reports=reports,
            )
            await self._set_run_status(run.id, RunStatus.COMPLETED, finished=True)
            await self._set_run_item(run.id, item_id, RunItemStatus.SUCCEEDED)
            output = result.get("output_paths", ())
            tracker.done(output[0] if output else None)
            self._publish_event(
                EventType.RUN_FINISHED,
                run.id,
                item_id=item_id,
                status=RunStatus.COMPLETED.value,
            )
            return await self._build_result(run.id, result)
        except asyncio.CancelledError:
            tracker.failed("cancelled by user")
            await self._cancel_run(run.id, "cancelled by user")
            raise
        except TrackForgeError as exc:
            tracker.failed(exc.message)
            await self._mark_failed(run.id, exc)
            raise
        except Exception as exc:
            tracker.failed(str(exc) or type(exc).__name__)
            await self._mark_failed(run.id, exc)
            raise
        finally:
            self._cancel_events.pop(str(run.id), None)

    async def run_item(
        self,
        item_id: ItemId,
        options: RunOptions | None = None,
        *,
        collection: CollectionItemContext | None = None,
        tracker: ProgressTracker | None = None,
    ) -> RunResult:
        """Run the remaining pipeline for an already acquired item."""

        opts = options or RunOptions()
        async with self._uow_factory() as uow:
            item = await uow.items.get(item_id)
            if item is None:
                raise RecordNotFound("item not found", context={"item_id": item_id})
            if collection is not None and item.collection_id is None:
                item.collection_id = collection.collection_id
                await uow.items.save(item)
            run = await uow.runs.create_run(opts.profile, opts.mode, status=RunStatus.PLANNED)
            await uow.runs.add_item(run.id, item_id, status=RunItemStatus.RUNNING)
        cancel = asyncio.Event()
        self._cancel_events[str(run.id)] = cancel
        if tracker is None:
            tracker = self._single_tracker(run.id, _item_label(item_id, collection))
        reports: list[HandlerReport] = []
        tracker.stage("plan", message="existing item")
        reports.append(
            self._finish_stage(
                tracker,
                "plan",
                utc_now(),
                time.monotonic(),
                summary="existing item",
            )
        )
        acquire_cached = item.media_id is not None
        tracker.stage("acquire", message="existing acquisition")
        acquire_summary, acquire_details = await self._acquire_summary(
            item_id,
            reused=True,
        )
        reports.append(
            self._finish_stage(
                tracker,
                "acquire",
                utc_now(),
                time.monotonic(),
                summary=acquire_summary,
                details=acquire_details,
                status=HandlerStatus.CACHED if acquire_cached else HandlerStatus.SUCCEEDED,
                cache_hit=acquire_cached,
            )
        )
        self._publish_event(EventType.RUN_STARTED, run.id, item_id=item_id)
        try:
            await self._set_run_status(run.id, RunStatus.RUNNING, started=True)
            result = await self._continue_item(
                run.id,
                item_id,
                opts,
                acquire_cached=acquire_cached,
                cancel=cancel,
                collection=collection,
                tracker=tracker,
                handler_reports=reports,
            )
            await self._set_run_status(run.id, RunStatus.COMPLETED, finished=True)
            await self._set_run_item(run.id, item_id, RunItemStatus.SUCCEEDED)
            output = result.get("output_paths", ())
            tracker.done(output[0] if output else None)
            self._publish_event(
                EventType.RUN_FINISHED,
                run.id,
                item_id=item_id,
                status=RunStatus.COMPLETED.value,
            )
            return await self._build_result(run.id, result)
        except asyncio.CancelledError:
            tracker.failed("cancelled by user")
            await self._cancel_run(run.id, "cancelled by user")
            raise
        except TrackForgeError as exc:
            tracker.failed(exc.message)
            await self._mark_failed(run.id, exc)
            raise
        except Exception as exc:
            tracker.failed(str(exc) or type(exc).__name__)
            await self._mark_failed(run.id, exc)
            raise
        finally:
            self._cancel_events.pop(str(run.id), None)

    async def run_candidate(
        self,
        candidate: Candidate,
        options: RunOptions | None = None,
        *,
        collection: CollectionItemContext | None = None,
        on_acquired: Callable[[ItemId], Awaitable[None]] | None = None,
        tracker: ProgressTracker | None = None,
    ) -> RunResult | PlanView:
        """Acquire and process one already-enumerated collection candidate."""

        opts = options or RunOptions()
        if opts.dry_run:
            return PlanView(source=candidate.source_ref, candidates=(candidate,))
        async with self._uow_factory() as uow:
            run = await uow.runs.create_run(opts.profile, opts.mode, status=RunStatus.PLANNED)
        cancel = asyncio.Event()
        self._cancel_events[str(run.id)] = cancel
        if tracker is None:
            tracker = self._single_tracker(run.id, _candidate_label(candidate))
        reports: list[HandlerReport] = []
        tracker.stage("plan", message="candidate planned")
        reports.append(
            self._finish_stage(
                tracker,
                "plan",
                utc_now(),
                time.monotonic(),
                summary="candidate planned",
            )
        )
        self._publish_event(EventType.RUN_STARTED, run.id, status=RunStatus.PLANNED.value)
        try:
            await self._set_run_status(run.id, RunStatus.RUNNING, started=True)
            tracker.stage("acquire", message="starting download")
            acquire_started_at = utc_now()
            acquire_started = time.monotonic()
            try:
                item_id = await self._acquire_candidate_for_run(
                    run.id,
                    candidate,
                    opts,
                    collection=collection,
                    on_acquired=on_acquired,
                    progress=tracker.percent,
                )
            except BaseException:
                tracker.failed("acquisition failed")
                raise
            tracker.bind(item_id=item_id)
            acquire_summary, acquire_details = await self._acquire_summary(
                item_id,
                reused=False,
            )
            reports.append(
                self._finish_stage(
                    tracker,
                    "acquire",
                    acquire_started_at,
                    acquire_started,
                    summary=acquire_summary,
                    details=acquire_details,
                )
            )
            await self._set_run_item(run.id, item_id, RunItemStatus.RUNNING)
            result = await self._continue_item(
                run.id,
                item_id,
                opts,
                acquire_cached=False,
                cancel=cancel,
                collection=collection,
                tracker=tracker,
                handler_reports=reports,
            )
            await self._set_run_status(run.id, RunStatus.COMPLETED, finished=True)
            await self._set_run_item(run.id, item_id, RunItemStatus.SUCCEEDED)
            output = result.get("output_paths", ())
            tracker.done(output[0] if output else None)
            self._publish_event(
                EventType.RUN_FINISHED,
                run.id,
                item_id=item_id,
                status=RunStatus.COMPLETED.value,
            )
            return await self._build_result(run.id, result)
        except asyncio.CancelledError:
            tracker.failed("cancelled by user")
            await self._cancel_run(run.id, "cancelled by user")
            raise
        except TrackForgeError as exc:
            tracker.failed(exc.message)
            await self._mark_failed(run.id, exc)
            raise
        except Exception as exc:
            tracker.failed(str(exc) or type(exc).__name__)
            await self._mark_failed(run.id, exc)
            raise
        finally:
            self._cancel_events.pop(str(run.id), None)

    async def _acquire_candidate_for_run(
        self,
        run_id: RunId,
        candidate: Candidate,
        options: RunOptions,
        *,
        collection: CollectionItemContext | None,
        on_acquired: Callable[[ItemId], Awaitable[None]] | None,
        progress: Callable[[float, str | None], None] | None = None,
    ) -> ItemId:
        selected = _candidate_with_format(candidate, options.format_id)
        acquired = await self._acquire_item.acquire(
            selected,
            AcquireOptions(
                format_id=options.format_id,
                offline=options.offline,
                force="acquire" in options.force_steps,
                progress=progress,
            ),
            self._acquirers,
        )
        await self._add_run_item(run_id, acquired.item_id)
        if collection is not None:
            async with self._uow_factory() as uow:
                item = await uow.items.get(acquired.item_id)
                if item is not None and item.collection_id is None:
                    item.collection_id = collection.collection_id
                    await uow.items.save(item)
        if on_acquired is not None:
            await on_acquired(acquired.item_id)
        return acquired.item_id

    async def resume(
        self,
        run_id: RunId,
        options: RunOptions | None = None,
    ) -> RunResult:
        """Continue a failed/paused run from its persisted item state."""

        async with self._uow_factory() as uow:
            run = await uow.runs.get_run(run_id)
            if run is None:
                raise RecordNotFound("run not found", context={"run_id": run_id})
            if run.status is RunStatus.COMPLETED:
                return await self._build_result(run_id, {})
            run_items = await _list_run_items(uow, run_id)
        if not run_items:
            raise RecordNotFound(
                "run has no item to resume",
                user_action="Import the source again; the acquisition did not complete.",
                context={"run_id": run_id},
            )
        item_id = run_items[0].item_id
        opts = options or RunOptions(profile=run.profile, mode=run.mode)
        cancel = asyncio.Event()
        self._cancel_events[str(run_id)] = cancel
        try:
            await self._set_run_status(run_id, RunStatus.RUNNING, started=True)
            self._publish_event(EventType.RUN_STARTED, run_id, item_id=item_id)
            await self._set_run_item(run_id, item_id, RunItemStatus.RUNNING)
            result = await self._continue_item(
                run_id,
                item_id,
                opts,
                acquire_cached=True,
                cancel=cancel,
            )
            await self._set_run_status(run_id, RunStatus.COMPLETED, finished=True)
            await self._set_run_item(run_id, item_id, RunItemStatus.SUCCEEDED)
            self._publish_event(
                EventType.RUN_FINISHED,
                run_id,
                item_id=item_id,
                status=RunStatus.COMPLETED.value,
            )
            return await self._build_result(run_id, result)
        except asyncio.CancelledError:
            await self._cancel_run(run_id, "cancelled by user")
            raise
        except TrackForgeError as exc:
            await self._mark_failed(run_id, exc)
            raise
        except Exception as exc:
            await self._mark_failed(run_id, exc)
            raise
        finally:
            self._cancel_events.pop(str(run_id), None)

    async def status(self, run_id: RunId) -> StatusView:
        """Return the persisted state of ``run_id``."""

        async with self._uow_factory() as uow:
            run = await uow.runs.get_run(run_id)
            if run is None:
                raise RecordNotFound("run not found", context={"run_id": run_id})
            steps = await uow.runs.list_steps(run_id)
            items = await _list_run_items(uow, run_id)
        return StatusView(
            run_id=run.id,
            status=run.status,
            profile=run.profile,
            mode=run.mode,
            steps=tuple(_step_view(step) for step in steps),
            items=tuple((record.item_id, record.status) for record in items),
            started_at=run.started_at,
            finished_at=run.finished_at,
            error=run.error,
        )

    def cancel(self, run_id: RunId) -> bool:
        """Request cooperative cancellation of an in-process run."""

        event = self._cancel_events.get(str(run_id))
        if event is None:
            return False
        event.set()
        return True

    async def _acquire_for_run(
        self,
        run_id: RunId,
        source: SourceRef,
        plan: PlanView,
        options: RunOptions,
        *,
        progress: Callable[[float, str | None], None] | None = None,
    ) -> tuple[ItemId | None, bool]:
        reusable = None
        selected_format = options.format_id
        if selected_format is None and plan.candidates:
            raw_selected = plan.candidates[0].extra.get("selected_format_id")
            if isinstance(raw_selected, str) and raw_selected:
                selected_format = raw_selected
        if "acquire" not in options.force_steps:
            reusable = await self._find_reusable_item(source, selected_format)
        if reusable is not None:
            await self._add_run_item(run_id, reusable.id)
            return reusable.id, True

        candidate = _candidate_with_format(plan.candidates[0], options.format_id)
        acquired = await self._acquire_item.acquire(
            candidate,
            AcquireOptions(
                format_id=options.format_id,
                offline=options.offline,
                force="acquire" in options.force_steps,
                progress=progress,
            ),
            self._acquirers,
        )
        await self._add_run_item(run_id, acquired.item_id)
        return acquired.item_id, False

    async def _add_run_item(self, run_id: RunId, item_id: ItemId) -> None:
        async with self._uow_factory() as uow:
            await uow.runs.add_item(run_id, item_id, status=RunItemStatus.PENDING)

    async def _find_reusable_item(
        self,
        source: SourceRef,
        selected_format: str | None = None,
    ) -> Item | None:
        if isinstance(source, Url):
            return await self._find_reusable_url_item(source, selected_format)
        if not isinstance(source, LocalPath):
            return None
        path = absolute_local_path(source.path)
        try:
            stat = path.stat()
        except OSError:
            return None
        async with self._uow_factory() as uow:
            instances = await uow.file_instances.find_by_path(str(path))
            for instance in reversed(instances):
                if instance.role is not FileRole.SOURCE:
                    continue
                if instance.size_bytes != stat.st_size:
                    continue
                recorded_mtime = (
                    instance.mtime.timestamp()
                    if isinstance(instance.mtime, datetime)
                    else float(instance.mtime)
                )
                if abs(recorded_mtime - stat.st_mtime) > 1.0:
                    continue
                candidates = await uow.items.find_by_media(instance.media_id)
                usable = [
                    item
                    for item in candidates
                    if item.status
                    not in {ItemStatus.FAILED, ItemStatus.ARCHIVED, ItemStatus.DUPLICATE}
                ]
                if usable:
                    return max(usable, key=lambda item: (item.created_at, str(item.id)))
        return None

    async def _find_reusable_url_item(
        self,
        source: Url,
        selected_format: str | None = None,
    ) -> Item | None:
        async with self._uow_factory() as uow:
            finder = getattr(uow.intakes, "find_by_source_ref", None)
            if not callable(finder):
                return None
            intakes = await finder(source)
            for intake in reversed(tuple(intakes)):
                if intake.status is not IntakeStatus.SUCCEEDED:
                    continue
                stored_format = intake.candidate.extra.get("selected_format_id")
                if isinstance(stored_format, str) and selected_format is not None:
                    if stored_format != selected_format:
                        continue
                item = await uow.items.get(intake.item_id)
                if item is None:
                    continue
                if item.status in {
                    ItemStatus.FAILED,
                    ItemStatus.ARCHIVED,
                    ItemStatus.DUPLICATE,
                }:
                    continue
                if item.media_id is None:
                    continue
                media = await uow.media.get(item.media_id)
                if media is None:
                    continue
                if not Path(media.cas_path).is_file():
                    continue
                return item
        return None

    async def _continue_item(
        self,
        run_id: RunId,
        item_id: ItemId,
        options: RunOptions,
        *,
        acquire_cached: bool,
        cancel: asyncio.Event,
        collection: CollectionItemContext | None = None,
        tracker: ProgressTracker | None = None,
        handler_reports: list[HandlerReport] | None = None,
    ) -> dict[str, Any]:
        reports = handler_reports if handler_reports is not None else []
        if tracker is None:
            tracker = self._single_tracker(run_id, str(item_id))
        steps = await self._ensure_steps(run_id, item_id)
        cached_steps: list[str] = []
        observation_warnings: list[str] = []
        await self._finish_step(
            steps["plan"],
            StepStatus.CACHED if acquire_cached else StepStatus.SUCCEEDED,
        )
        if acquire_cached:
            cached_steps.append("plan")
        if acquire_cached:
            await self._finish_step(steps["acquire"], StepStatus.CACHED)
            cached_steps.append("acquire")
        else:
            await self._finish_step(steps["acquire"], StepStatus.SUCCEEDED)

        async with self._uow_factory() as uow:
            item = await uow.items.get(item_id)
            if item is None:
                raise RecordNotFound("item not found", context={"item_id": item_id})
            if item.media_id is None:
                raise UnsupportedSource(
                    "item has no acquired media",
                    user_action="Import the source again.",
                    context={"item_id": item_id},
                )
            intakes = await uow.intakes.find_by_item(item_id)
            is_url_source = any(isinstance(intake.source_ref, Url) for intake in intakes)

        observe_started_at = utc_now()
        observe_started = time.monotonic()
        if tracker is not None:
            tracker.stage("observe", message="observe")
        if (
            item.status is ItemStatus.ACQUIRED
            or "observe" in options.force_steps
            or options.refresh
        ):
            self._check_cancelled(cancel)
            observation = await self._run_stage(
                steps["observe"],
                self._observe_item.observe(
                    item_id,
                    options.profile,
                    refresh=options.refresh,
                    enabled=options.enabled,
                    disabled=options.disabled,
                    progress=tracker,
                    progress_context=(tracker.context if tracker is not None else None),
                ),
            )
            reports.extend(observation.step_reports)
            observation_warnings.extend(
                warning.message
                for warning in observation.warnings
                if not (is_url_source and warning.code == "path_unavailable")
            )
            await self._finish_step(steps["observe"], StepStatus.SUCCEEDED)
            self._publish_event(
                EventType.STEP_FINISHED,
                run_id,
                item_id=item_id,
                capability="observe",
            )
            reports.append(
                self._finish_stage(
                    tracker,
                    "observe",
                    observe_started_at,
                    observe_started,
                    summary=(
                        f"{len(observation.steps)} capability step(s), "
                        f"{len(observation.artifacts)} artifact(s)"
                    ),
                    details={
                        "steps": len(observation.steps),
                        "artifacts": len(observation.artifacts),
                    },
                    artifacts=tuple(artifact.type for artifact in observation.artifacts),
                )
            )
        else:
            await self._finish_step(steps["observe"], StepStatus.CACHED)
            cached_steps.append("observe")
            reports.append(
                self._finish_stage(
                    tracker,
                    "observe",
                    observe_started_at,
                    observe_started,
                    summary="existing observations reused",
                    status=HandlerStatus.CACHED,
                    cache_hit=True,
                )
            )

        interpret_started_at = utc_now()
        interpret_started = time.monotonic()
        if tracker is not None:
            tracker.stage("interpret", message="interpret")
        if (
            item.status in {ItemStatus.ACQUIRED, ItemStatus.OBSERVED}
            or "interpret" in options.force_steps
            or options.refresh
        ):
            self._check_cancelled(cancel)
            interpretation = await self._run_stage(
                steps["interpret"],
                self._interpret_item.interpret(item_id),
            )
            await self._finish_step(steps["interpret"], StepStatus.SUCCEEDED)
            reports.append(
                self._finish_stage(
                    tracker,
                    "interpret",
                    interpret_started_at,
                    interpret_started,
                    summary=_interpretation_summary(interpretation),
                    details={"claims": len(interpretation.claims)},
                    warnings=interpretation.warnings,
                )
            )
        else:
            await self._finish_step(steps["interpret"], StepStatus.CACHED)
            cached_steps.append("interpret")
            reports.append(
                self._finish_stage(
                    tracker,
                    "interpret",
                    interpret_started_at,
                    interpret_started,
                    summary="existing interpretation reused",
                    status=HandlerStatus.CACHED,
                    cache_hit=True,
                )
            )

        resolve_started_at = utc_now()
        resolve_started = time.monotonic()
        if tracker is not None:
            tracker.stage("resolve", message="resolve")
        self._check_cancelled(cancel)
        resolved = await self._run_stage(
            steps["resolve"],
            self._resolve_item.resolve(item_id, options.mode),
        )
        await self._finish_step(steps["resolve"], StepStatus.SUCCEEDED)
        tag_basis: TagBasisApplication | None = None
        if collection is not None:
            self._check_cancelled(cancel)
            tag_basis = await self._tag_basis.apply(
                self._uow_factory,
                item_id,
                collection,
                mode=options.mode,
            )
            resolved = tag_basis.resolved
        final_title = resolved.fields.get("title")
        if tracker is not None and isinstance(final_title, str) and final_title.strip():
            tracker.bind(label=final_title)
        reports.append(
            self._finish_stage(
                tracker,
                "resolve",
                resolve_started_at,
                resolve_started,
                summary=(
                    f"{len(resolved.fields)} field(s), {len(resolved.unknowns)} unknown "
                    f"field(s), {len(resolved.warnings)} warning(s)"
                ),
                details={
                    "fields": len(resolved.fields),
                    "unknowns": len(resolved.unknowns),
                },
                warnings=resolved.warnings,
            )
        )
        # Warnings are rebuilt from the final decision record: a stale
        # "album/artist unknown" warning from the pre-tag-basis resolution
        # must not survive into the CLI or sidecar.
        warnings: list[str] = [
            *observation_warnings,
            *resolved.warnings,
            *((*tag_basis.warnings,) if tag_basis is not None else ()),
        ]

        render: Render | None = None
        bundle: OutputBundle | None = None
        render_started_at = utc_now()
        render_started = time.monotonic()
        if tracker is not None:
            tracker.stage("render", message="render")
        reuse = (
            None
            if collection is not None
            else await self._reusable_render(item_id, resolved.fields, options)
        )
        if reuse is not None:
            render, bundle = reuse
            await self._finish_step(steps["render"], StepStatus.CACHED)
            cached_steps.append("render")
            reports.append(
                self._finish_stage(
                    tracker,
                    "render",
                    render_started_at,
                    render_started,
                    summary=_render_summary(render, bundle),
                    status=HandlerStatus.CACHED,
                    cache_hit=True,
                )
            )
        else:
            self._check_cancelled(cancel)
            render_options = RenderOptions(target_format=options.target_format)
            rendered = await self._run_stage(
                steps["render"],
                self._render_item.render(
                    item_id,
                    render_options,
                    collection=(
                        _sidecar_collection(collection, tag_basis)
                        if collection is not None and tag_basis is not None
                        else None
                    ),
                    tag_basis=(tag_basis.basis.value if tag_basis is not None else None),
                    tag_alternatives=(tag_basis.alternatives if tag_basis is not None else None),
                    final_warnings=tuple(dict.fromkeys(warnings)),
                ),
            )
            render, bundle = rendered.render, rendered.bundle
            warnings.extend(rendered.warnings)
            await self._finish_step(steps["render"], StepStatus.SUCCEEDED)
            reports.append(
                self._finish_stage(
                    tracker,
                    "render",
                    render_started_at,
                    render_started,
                    summary=_render_summary(render, bundle),
                    details={"target_format": rendered.render.target_format.value},
                    artifacts=(str(rendered.bundle.id),),
                    warnings=rendered.warnings,
                )
            )
        if bundle is None:  # pragma: no cover - defensive
            raise RenderFailed("render produced no bundle", context={"item_id": item_id})

        publish_started_at = utc_now()
        publish_started = time.monotonic()
        if tracker is not None:
            tracker.stage("publish", message="publish")
        self._check_cancelled(cancel)
        publish_options = options.publish
        if collection is not None:
            publish_options = replace(
                publish_options,
                subfolder=collection.title,
            )
        publish_result = await self._run_stage(
            steps["publish"],
            self._publish_item.publish(
                item_id,
                publish_options,
                bundle_id=str(bundle.id),
                library_root=self._library_root,
                naming=options.naming,
                force="publish" in options.force_steps,
            ),
        )
        await self._finish_step(
            steps["publish"],
            StepStatus.CACHED if publish_result.cache_hit else StepStatus.SUCCEEDED,
        )
        if publish_result.cache_hit:
            cached_steps.append("publish")
        reports.append(
            self._finish_stage(
                tracker,
                "publish",
                publish_started_at,
                publish_started,
                summary=publish_result.path,
                details={"path": publish_result.path},
                status=(
                    HandlerStatus.CACHED if publish_result.cache_hit else HandlerStatus.SUCCEEDED
                ),
                cache_hit=publish_result.cache_hit,
            )
        )
        return {
            "item_id": item_id,
            "cached_steps": cached_steps,
            "output_paths": [publish_result.path],
            "warnings": [*dict.fromkeys(warnings), *[f"cache_hit:{name}" for name in cached_steps]],
            "publish": publish_result,
            "resolve": resolved,
            "render": render,
            "tag_basis": tag_basis,
            "collection": collection,
            "handler_reports": tuple(reports),
        }

    def _single_tracker(self, run_id: RunId, label: str) -> ProgressTracker:
        scope = f"run:{run_id}"
        context = ProgressContext(
            scope_id=scope,
            collection_id=scope,
            item_id=None,
            position=1,
            total=1,
            label=label or "item 1",
            stage="plan",
        )
        self._progress.start_collection(scope, (context.queued(),))
        return ProgressTracker(self._progress, context)

    def _finish_stage(
        self,
        tracker: ProgressTracker | None,
        stage: str,
        started_at: datetime,
        started: float,
        *,
        summary: str,
        details: Mapping[str, Any] | None = None,
        status: HandlerStatus = HandlerStatus.SUCCEEDED,
        artifacts: Sequence[str] = (),
        warnings: Sequence[str] = (),
        cache_hit: bool = False,
    ) -> HandlerReport:
        if tracker is None:  # pragma: no cover - callers always bind a tracker
            tracker = self._single_tracker(RunId(str(started_at.timestamp())), summary)
        progress_status = (
            ProgressStatus.CACHED
            if cache_hit or status is HandlerStatus.CACHED
            else ProgressStatus.SKIPPED
            if status is HandlerStatus.SKIPPED
            else ProgressStatus.FAILED
            if status is HandlerStatus.FAILED
            else ProgressStatus.DONE
        )
        tracker.stage(stage, status=progress_status)
        return tracker.report(
            capability_id=stage,
            status=status,
            started_at=started_at,
            duration_ms=int((time.monotonic() - started) * 1000),
            kind="stage",
            stage=stage,
            summary=summary,
            details=details,
            artifacts=artifacts,
            warnings=warnings,
            cache_hit=cache_hit,
        )

    async def _acquire_summary(
        self,
        item_id: ItemId,
        *,
        reused: bool,
    ) -> tuple[str, dict[str, Any]]:
        async with self._uow_factory() as uow:
            item = await uow.items.get(item_id)
            media = (
                await uow.media.get(item.media_id)
                if item is not None and item.media_id is not None
                else None
            )
        if media is None:
            return ("reused acquisition" if reused else "acquired", {})
        size_mb = media.size_bytes / 1_000_000
        duration = media.duration_seconds
        parts = [media.container.value, f"{size_mb:.1f} MB"]
        if duration is not None:
            parts.append(f"{duration:.1f} s")
        summary = ", ".join(parts)
        details: dict[str, Any] = {
            "container": media.container.value,
            "size_bytes": media.size_bytes,
        }
        if duration is not None:
            details["duration_seconds"] = round(float(duration), 3)
        if reused:
            details["reused"] = True
        return summary, details

    async def _reusable_render(
        self,
        item_id: ItemId,
        resolved_fields: dict[str, Any],
        options: RunOptions,
    ) -> tuple[Render, OutputBundle] | None:
        if "render" in options.force_steps:
            return None
        async with self._uow_factory() as uow:
            render = await _latest_render(uow, item_id)
            if render is None:
                return None
            if (
                options.target_format is not None
                and render.target_format is not options.target_format
            ):
                return None
            previous = await uow.decisions.get_record(render.decision_record_id)
            if previous is None:
                return None
            previous_fields = {
                decision.field: decision.value
                for decision in previous.decisions
                if decision.subject is Subject.ITEM
            }
            if previous_fields != resolved_fields:
                return None
            bundle = await _find_bundle_by_render(uow, str(render.id))
            if bundle is None:
                return None
        return render, bundle

    async def _ensure_steps(self, run_id: RunId, item_id: ItemId) -> dict[str, StepRecord]:
        async with self._uow_factory() as uow:
            existing = {
                step.capability_id: step
                for step in await uow.runs.list_steps(run_id, item_id=item_id)
            }
            result = dict(existing)
            for capability in _STEP_CAPABILITIES:
                if capability in result:
                    continue
                result[capability] = await uow.runs.create_step(
                    run_id,
                    item_id,
                    capability,
                    _STEP_VERSIONS[capability],
                )
            return result

    async def _start_step(self, step: StepRecord) -> None:
        async with self._uow_factory() as uow:
            await uow.runs.update_step(
                step.id,
                status=StepStatus.RUNNING,
                attempts=step.attempts + 1,
                started_at=utc_now(),
            )
        self._publish_event(
            EventType.STEP_STARTED,
            step.run_id,
            item_id=step.item_id,
            step_id=step.id,
            capability=step.capability_id,
        )

    async def _finish_step(
        self,
        step: StepRecord,
        status: StepStatus,
        *,
        warnings: Sequence[str] = (),
    ) -> None:
        async with self._uow_factory() as uow:
            await uow.runs.update_step(
                step.id,
                status=status,
                finished_at=utc_now(),
                warnings=tuple(dict.fromkeys(warnings)),
            )
        event_type = (
            EventType.STEP_CACHED
            if status is StepStatus.CACHED
            else EventType.STEP_SKIPPED
            if status is StepStatus.SKIPPED
            else EventType.STEP_FINISHED
        )
        self._publish_event(
            event_type,
            step.run_id,
            item_id=step.item_id,
            step_id=step.id,
            capability=step.capability_id,
            status=status.value,
        )

    async def _run_stage(
        self,
        step: StepRecord,
        operation: Awaitable[T],
    ) -> T:
        await self._start_step(step)
        try:
            result = await operation
        except BaseException as exc:
            await self._fail_step(step, exc)
            raise
        return result

    async def _fail_step(self, step: StepRecord, exc: BaseException) -> None:
        async with self._uow_factory() as uow:
            await uow.runs.update_step(
                step.id,
                status=StepStatus.FAILED,
                finished_at=utc_now(),
                error=_error_dict(exc),
            )
        self._publish_event(
            EventType.STEP_FAILED,
            step.run_id,
            item_id=step.item_id,
            step_id=step.id,
            capability=step.capability_id,
            status=StepStatus.FAILED.value,
            message=str(exc),
        )

    async def _set_run_status(
        self,
        run_id: RunId,
        status: RunStatus,
        *,
        started: bool = False,
        finished: bool = False,
    ) -> None:
        async with self._uow_factory() as uow:
            await uow.runs.update_run(
                run_id,
                status=status,
                started_at=utc_now() if started else None,
                finished_at=utc_now() if finished else None,
                error={} if status in {RunStatus.RUNNING, RunStatus.COMPLETED} else None,
            )

    async def _set_run_item(
        self,
        run_id: RunId,
        item_id: ItemId,
        status: RunItemStatus,
    ) -> None:
        async with self._uow_factory() as uow:
            await uow.runs.set_item_status(run_id, item_id, status)

    async def _mark_failed(self, run_id: RunId, exc: BaseException) -> None:
        error = _error_dict(exc)
        async with self._uow_factory() as uow:
            await uow.runs.update_run(
                run_id,
                status=RunStatus.FAILED,
                finished_at=utc_now(),
                error=error,
            )
            for record in await _list_run_items(uow, run_id):
                await uow.runs.set_item_status(run_id, record.item_id, RunItemStatus.FAILED)
        self._publish_event(
            EventType.RUN_FAILED,
            run_id,
            status=RunStatus.FAILED.value,
            message=str(exc),
        )

    async def _cancel_run(self, run_id: RunId, message: str) -> None:
        async with self._uow_factory() as uow:
            await uow.runs.update_run(
                run_id,
                status=RunStatus.CANCELLED,
                finished_at=utc_now(),
                error={"type": "Cancelled", "message": message},
            )
        self._publish_event(
            EventType.RUN_CANCELLED,
            run_id,
            status=RunStatus.CANCELLED.value,
            message=message,
        )

    def _check_cancelled(self, cancel: asyncio.Event) -> None:
        if cancel.is_set():
            raise asyncio.CancelledError()

    def _publish_event(
        self,
        event_type: EventType,
        run_id: RunId,
        *,
        item_id: ItemId | None = None,
        step_id: Any | None = None,
        capability: str | None = None,
        status: str | None = None,
        message: str | None = None,
    ) -> None:
        try:
            self._events.publish(
                Event(
                    type=event_type,
                    run_id=run_id,
                    item_id=item_id,
                    step_id=step_id,
                    capability=capability,
                    status=status,
                    message=message,
                )
            )
        except Exception:
            # Observability must never break the pipeline.
            return

    async def _build_result(
        self,
        run_id: RunId,
        state: dict[str, Any],
    ) -> RunResult:
        async with self._uow_factory() as uow:
            run = await uow.runs.get_run(run_id)
            if run is None:  # pragma: no cover - the run was just created
                raise RecordNotFound("run not found", context={"run_id": run_id})
            records = await _list_run_items(uow, run_id)
            steps = await uow.runs.list_steps(run_id)
            item_views: list[RunItemView] = []
            output_paths: list[str] = []
            warnings: list[str] = []
            for record in records:
                item = await uow.items.get(record.item_id)
                if item is None:
                    continue
                decisions = await _latest_decisions(uow, item)
                entries = await uow.library.list_entries(item_id=item.id)
                published = [entry for entry in entries if entry.status.value == "published"]
                output_path = published[-1].path if published else None
                if output_path:
                    output_paths.append(output_path)
                warnings.extend(state.get("warnings", ()))
                item_views.append(
                    RunItemView(
                        item_id=item.id,
                        status=item.status,
                        identity_summary={
                            "kind": item.kind.value if item.kind is not None else None,
                            "media_id": str(item.media_id) if item.media_id else None,
                        },
                        decisions_summary=decisions,
                        output_path=output_path,
                        warnings=tuple(dict.fromkeys(state.get("warnings", ()))),
                        cached_steps=tuple(state.get("cached_steps", ())),
                    )
                )
        return RunResult(
            run_id=run.id,
            status=run.status,
            items=tuple(item_views),
            warnings=tuple(dict.fromkeys(warnings)),
            output_paths=tuple(dict.fromkeys(output_paths)),
            steps=_step_views(steps, tuple(state.get("handler_reports", ()))),
            cache_hit=bool(state.get("cached_steps")),
            handler_reports=tuple(state.get("handler_reports", ())),
        )


def _plan_label(plan: PlanView, source: SourceRef) -> str:
    if plan.candidates and plan.candidates[0].title.strip():
        return plan.candidates[0].title
    if isinstance(source, LocalPath):
        return Path(source.path).name or source.path
    if isinstance(source, Url):
        return _candidate_label(plan.candidates[0]) if plan.candidates else source.url
    return type(source).__name__


def _candidate_label(candidate: Candidate, position: int = 1) -> str:
    title = candidate.title.strip() if isinstance(candidate.title, str) else ""
    if title:
        return title
    source_ref = candidate.source_ref
    if isinstance(source_ref, LocalPath):
        name = Path(source_ref.path).name
        if name:
            return name
    external_id = getattr(candidate, "external_id", "")
    if isinstance(external_id, str) and external_id.strip():
        return external_id
    return f"item {position}"


def _item_label(item_id: ItemId, collection: CollectionItemContext | None) -> str:
    if collection is not None:
        return f"{collection.title} #{collection.position}"
    return str(item_id)


def _interpretation_summary(interpretation: Any) -> str:
    claims = len(getattr(interpretation, "claims", ()) or ())
    derivative = getattr(interpretation, "derivative", None)
    names: list[str] = []
    if isinstance(derivative, Mapping):
        hypotheses = derivative.get("hypotheses")
        if isinstance(hypotheses, list) and hypotheses:
            first = hypotheses[0]
            if isinstance(first, Mapping):
                transformations = first.get("transformations")
                if isinstance(transformations, list):
                    names.extend(str(item).strip() for item in transformations if str(item).strip())
                elif isinstance(transformations, str) and transformations.strip():
                    names.append(transformations.strip())
        if not names:
            raw = derivative.get("derivative") or derivative.get("label") or derivative.get("type")
            if isinstance(raw, str) and raw.strip():
                names.append(raw.strip())
    elif isinstance(derivative, str) and derivative.strip():
        names.append(derivative.strip())
    if names:
        return f"{claims} claim(s), derivative={names[0]}"
    return f"{claims} claim(s)"


def _render_summary(render: Render, bundle: OutputBundle) -> str:
    target = getattr(render.target_format, "value", render.target_format)
    return f"{target}, bundle {bundle.bundle_hash[:4]}…"


def _sidecar_collection(
    context: CollectionItemContext,
    tag_basis: TagBasisApplication,
) -> SidecarCollection:
    return SidecarCollection(
        id=str(context.collection_id),
        title=context.title,
        kind=context.kind.value,
        owner=context.owner,
        position=context.position,
        tag_basis=tag_basis.basis.value,
        alternatives=tag_basis.alternatives,
        context={
            claim.field: claim.value for claim in context.context_claims if claim.value is not None
        },
    )


def resolve_local_import_path(path: str | Path) -> LocalPath:
    """Validate one user-supplied local import path without touching the catalog."""

    text = str(path).strip()
    if not text:
        raise UnsupportedSource("path must not be empty")
    if "://" in text:
        raise UnsupportedSource(
            "URL inputs are not available in the local MVP",
            user_action="Use a local audio file; URL/yt-dlp support arrives in Plan 11.",
            context={"source": text},
        )
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise UnsupportedSource(
            "local path does not exist",
            user_action="Pass the path to an existing supported audio file.",
            context={"path": str(candidate), "error": str(exc)},
        ) from exc
    if resolved.is_dir():
        raise UnsupportedSource(
            "folder/playlist import is not available in the local MVP",
            user_action="Pass one audio file; collection import arrives in Plan 18.",
            context={"path": str(resolved)},
        )
    if not resolved.is_file():
        raise UnsupportedSource(
            "local path is not a regular file",
            user_action="Pass the path to an existing supported audio file.",
            context={"path": str(resolved)},
        )
    return LocalPath(str(resolved))


class ImportPath:
    """Import one local file through the full pipeline."""

    def __init__(self, process_item: ProcessItem, plan_item: PlanItem) -> None:
        self._process_item = process_item
        self._plan_item = plan_item

    async def import_path(
        self,
        path: str | Path,
        options: RunOptions | None = None,
    ) -> RunResult | PlanView:
        opts = options or RunOptions()
        source = resolve_local_import_path(path)
        return await self._process_item.run(source, opts)


async def _list_run_items(uow: UnitOfWork, run_id: RunId) -> list[Any]:
    method = getattr(uow.runs, "list_items", None)
    if callable(method):
        reader = cast(_RunItemsReader, uow.runs)
        return await reader.list_items(run_id)
    # Compatibility with lightweight fake repositories that only implement
    # the original RunRepository port.  Persisted step item ids are enough to
    # resume and report status.
    from trackforge.application.dto.db import RunItemRecord

    seen: dict[str, RunItemRecord] = {}
    for step in await uow.runs.list_steps(run_id):
        key = str(step.item_id)
        seen.setdefault(key, RunItemRecord(run_id=run_id, item_id=step.item_id))
    return list(seen.values())


async def _find_bundle_by_render(uow: UnitOfWork, render_id: str) -> OutputBundle | None:
    reader = cast(_BundleReader, uow.renders)
    return await reader.find_bundle_by_render(render_id)


async def _latest_render(uow: UnitOfWork, item_id: ItemId) -> Render | None:
    renders = await uow.renders.list_by_item(item_id)
    return renders[-1] if renders else None


async def _latest_decisions(uow: UnitOfWork, item: Item) -> dict[str, Any]:
    if item.decision_record_id is not None:
        record = await uow.decisions.get_record(item.decision_record_id)
        if record is not None:
            return {
                decision.field: decision.value
                for decision in record.decisions
                if decision.subject is Subject.ITEM
            }
    records = await uow.decisions.list_records(item.id)
    if not records:
        return {}
    return {
        decision.field: decision.value
        for decision in records[-1].decisions
        if decision.subject is Subject.ITEM
    }


def _candidate_with_format(candidate: Candidate, format_id: str | None) -> Candidate:
    if format_id is None:
        return candidate
    selected = next(
        (fmt for fmt in candidate.formats if fmt.format_id == format_id),
        None,
    )
    if selected is None:
        raise CandidateNotFound(
            "selected format is not offered by the source",
            user_action="Choose one of the formats shown by plan.",
            context={"format_id": format_id},
        )
    if selected.drm:
        raise DrmProtected(
            "the selected format is DRM protected",
            context={"format_id": format_id},
        )
    return replace(
        candidate,
        extra={**candidate.extra, "selected_format_id": format_id},
    )


def _plan_failure(plan: PlanView) -> TrackForgeError:
    reason = plan.blocked_reason or "source resolved to no candidates"
    warning = plan.warnings[0] if plan.warnings else reason
    if plan.candidates:
        return CandidateNotFound(reason, context={"source": type(plan.source).__name__})
    if "unsupported" in reason.casefold():
        return UnsupportedSource(
            warning,
            user_action="Use a local .mp3, .m4a, .flac, .ogg, or .opus file.",
            context={"reason": reason},
        )
    return CandidateNotFound(reason, context={"warnings": list(plan.warnings)})


def _step_views(
    steps: Sequence[StepRecord], reports: Sequence[HandlerReport]
) -> tuple[RunStepView, ...]:
    summaries = {report.capability_id: report for report in reports}
    return tuple(
        _step_view(
            step,
            report=summaries.get(step.capability_id),
        )
        for step in steps
    )


def _step_view(
    step: StepRecord,
    *,
    report: HandlerReport | None = None,
) -> RunStepView:
    warnings = tuple(
        warning if isinstance(warning, str) else str(warning.get("message", warning))
        for warning in step.warnings
    )
    duration_ms: int | None = None
    if step.started_at is not None and step.finished_at is not None:
        duration_ms = max(
            0,
            int((step.finished_at - step.started_at).total_seconds() * 1000),
        )
    return RunStepView(
        capability_id=step.capability_id,
        status=step.status,
        attempts=step.attempts,
        cache_key=step.cache_key,
        error=step.error,
        warnings=warnings,
        duration_ms=report.duration_ms if report is not None else duration_ms,
        summary=report.summary if report is not None else None,
        details=dict(report.details) if report is not None else {},
    )


def _error_dict(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, TrackForgeError):
        return exc.to_dict()
    return {
        "type": type(exc).__name__,
        "message": str(exc),
        "retryable": False,
        "user_action": None,
        "context": {},
    }


__all__ = ["ImportPath", "ProcessItem", "resolve_local_import_path"]
