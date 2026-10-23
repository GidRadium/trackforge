"""End-to-end local processing: plan → acquire → observe → … → publish.

``ProcessItem`` is the only application service that sequences the individual
use cases.  It owns run/step records, cancellation, cache reuse, and the
decision to publish an existing bundle instead of rendering it again.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, TypeVar, cast

from trackforge.application.dto.db import (
    RunItemStatus,
    RunStatus,
    StepRecord,
    StepStatus,
)
from trackforge.application.dto.plan import AcquireOptions, PlanOptions, PlanView
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
from trackforge.application.use_cases.acquire_item import AcquireItem
from trackforge.application.use_cases.interpret_item import InterpretItem
from trackforge.application.use_cases.observe_item import ObserveItem
from trackforge.application.use_cases.plan_item import PlanItem
from trackforge.application.use_cases.publish_item import PublishItem
from trackforge.application.use_cases.render_item import RenderItem
from trackforge.application.use_cases.resolve_item import ResolveItem
from trackforge.domain.errors import (
    CandidateNotFound,
    RecordNotFound,
    RenderFailed,
    TrackForgeError,
    UnsupportedSource,
)
from trackforge.domain.model._validation import utc_now
from trackforge.domain.model.claims import Subject
from trackforge.domain.model.ids import ItemId, RunId
from trackforge.domain.model.item import Item, ItemStatus
from trackforge.domain.model.media import FileRole
from trackforge.domain.model.outputs import OutputBundle, Render
from trackforge.domain.model.source import LocalPath, SourceRef
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
        self._cancel_events: dict[str, asyncio.Event] = {}

    @property
    def events(self) -> EventBus:
        return self._events

    @property
    def plan_item(self) -> PlanItem:
        return self._plan_item

    async def plan(self, source: SourceRef, options: RunOptions | None = None) -> PlanView:
        """Build the side-effect-free plan for ``source``."""

        opts = options or RunOptions()
        return await self._plan_item.plan(
            source,
            PlanOptions(profile=opts.profile),
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
        self._publish_event(EventType.RUN_STARTED, run.id, status=RunStatus.PLANNED.value)
        try:
            await self._set_run_status(run.id, RunStatus.RUNNING, started=True)
            item_id, acquire_reused = await self._acquire_for_run(run.id, source, plan, opts)
            if item_id is None:  # pragma: no cover - defensive
                raise UnsupportedSource("source could not be acquired")
            await self._set_run_item(run.id, item_id, RunItemStatus.RUNNING)
            result = await self._continue_item(
                run.id,
                item_id,
                opts,
                acquire_cached=acquire_reused,
                cancel=cancel,
            )
            await self._set_run_status(run.id, RunStatus.COMPLETED, finished=True)
            await self._set_run_item(run.id, item_id, RunItemStatus.SUCCEEDED)
            self._publish_event(
                EventType.RUN_FINISHED,
                run.id,
                item_id=item_id,
                status=RunStatus.COMPLETED.value,
            )
            return await self._build_result(run.id, result)
        except asyncio.CancelledError:
            await self._cancel_run(run.id, "cancelled by user")
            raise
        except TrackForgeError as exc:
            await self._mark_failed(run.id, exc)
            raise
        except Exception as exc:
            await self._mark_failed(run.id, exc)
            raise
        finally:
            self._cancel_events.pop(str(run.id), None)

    async def run_item(
        self,
        item_id: ItemId,
        options: RunOptions | None = None,
    ) -> RunResult:
        """Run the remaining pipeline for an already acquired item."""

        opts = options or RunOptions()
        async with self._uow_factory() as uow:
            item = await uow.items.get(item_id)
            if item is None:
                raise RecordNotFound("item not found", context={"item_id": item_id})
            run = await uow.runs.create_run(opts.profile, opts.mode, status=RunStatus.PLANNED)
            await uow.runs.add_item(run.id, item_id, status=RunItemStatus.RUNNING)
        cancel = asyncio.Event()
        self._cancel_events[str(run.id)] = cancel
        self._publish_event(EventType.RUN_STARTED, run.id, item_id=item_id)
        try:
            await self._set_run_status(run.id, RunStatus.RUNNING, started=True)
            result = await self._continue_item(
                run.id,
                item_id,
                opts,
                acquire_cached=item.media_id is not None,
                cancel=cancel,
            )
            await self._set_run_status(run.id, RunStatus.COMPLETED, finished=True)
            await self._set_run_item(run.id, item_id, RunItemStatus.SUCCEEDED)
            self._publish_event(
                EventType.RUN_FINISHED,
                run.id,
                item_id=item_id,
                status=RunStatus.COMPLETED.value,
            )
            return await self._build_result(run.id, result)
        except asyncio.CancelledError:
            await self._cancel_run(run.id, "cancelled by user")
            raise
        except TrackForgeError as exc:
            await self._mark_failed(run.id, exc)
            raise
        except Exception as exc:
            await self._mark_failed(run.id, exc)
            raise
        finally:
            self._cancel_events.pop(str(run.id), None)

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
    ) -> tuple[ItemId | None, bool]:
        reusable = None
        if "acquire" not in options.force_steps:
            reusable = await self._find_reusable_item(source)
        if reusable is not None:
            await self._add_run_item(run_id, reusable.id)
            return reusable.id, True

        candidate = plan.candidates[0]
        acquired = await self._acquire_item.acquire(
            candidate,
            AcquireOptions(),
            self._acquirers,
        )
        await self._add_run_item(run_id, acquired.item_id)
        return acquired.item_id, False

    async def _add_run_item(self, run_id: RunId, item_id: ItemId) -> None:
        async with self._uow_factory() as uow:
            await uow.runs.add_item(run_id, item_id, status=RunItemStatus.PENDING)

    async def _find_reusable_item(self, source: SourceRef) -> Item | None:
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

    async def _continue_item(
        self,
        run_id: RunId,
        item_id: ItemId,
        options: RunOptions,
        *,
        acquire_cached: bool,
        cancel: asyncio.Event,
    ) -> dict[str, Any]:
        steps = await self._ensure_steps(run_id, item_id)
        cached_steps: list[str] = []
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
        if item.status is ItemStatus.ACQUIRED or "observe" in options.force_steps:
            self._check_cancelled(cancel)
            await self._run_stage(
                steps["observe"],
                self._observe_item.observe(item_id, options.profile),
            )
            await self._finish_step(steps["observe"], StepStatus.SUCCEEDED)
            self._publish_event(
                EventType.STEP_FINISHED,
                run_id,
                item_id=item_id,
                capability="observe",
            )
        else:
            await self._finish_step(steps["observe"], StepStatus.CACHED)
            cached_steps.append("observe")

        if (
            item.status in {ItemStatus.ACQUIRED, ItemStatus.OBSERVED}
            or "interpret" in options.force_steps
        ):
            self._check_cancelled(cancel)
            await self._run_stage(
                steps["interpret"],
                self._interpret_item.interpret(item_id),
            )
            await self._finish_step(steps["interpret"], StepStatus.SUCCEEDED)
        else:
            await self._finish_step(steps["interpret"], StepStatus.CACHED)
            cached_steps.append("interpret")

        self._check_cancelled(cancel)
        resolved = await self._run_stage(
            steps["resolve"],
            self._resolve_item.resolve(item_id, options.mode),
        )
        await self._finish_step(steps["resolve"], StepStatus.SUCCEEDED)
        warnings: list[str] = list(resolved.warnings)

        render: Render | None = None
        bundle: OutputBundle | None = None
        reuse = await self._reusable_render(item_id, resolved.fields, options)
        if reuse is not None:
            render, bundle = reuse
            await self._finish_step(steps["render"], StepStatus.CACHED)
            cached_steps.append("render")
        else:
            self._check_cancelled(cancel)
            render_options = RenderOptions(target_format=options.target_format)
            rendered = await self._run_stage(
                steps["render"],
                self._render_item.render(item_id, render_options),
            )
            render, bundle = rendered.render, rendered.bundle
            warnings.extend(rendered.warnings)
            await self._finish_step(steps["render"], StepStatus.SUCCEEDED)
        if bundle is None:  # pragma: no cover - defensive
            raise RenderFailed("render produced no bundle", context={"item_id": item_id})

        self._check_cancelled(cancel)
        publish_result = await self._run_stage(
            steps["publish"],
            self._publish_item.publish(
                item_id,
                options.publish,
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
        return {
            "item_id": item_id,
            "cached_steps": cached_steps,
            "output_paths": [publish_result.path],
            "warnings": [*dict.fromkeys(warnings), *[f"cache_hit:{name}" for name in cached_steps]],
            "publish": publish_result,
            "resolve": resolved,
            "render": render,
        }

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
            steps=tuple(_step_view(step) for step in steps),
            cache_hit=bool(state.get("cached_steps")),
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


def _step_view(step: StepRecord) -> RunStepView:
    warnings = tuple(
        warning if isinstance(warning, str) else str(warning.get("message", warning))
        for warning in step.warnings
    )
    return RunStepView(
        capability_id=step.capability_id,
        status=step.status,
        attempts=step.attempts,
        cache_key=step.cache_key,
        error=step.error,
        warnings=warnings,
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
