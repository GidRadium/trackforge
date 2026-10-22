"""Asynchronous capability executor with cache, retry, fallback, and resume.

The executor is deliberately generic: it knows capability manifests and step
state, never concrete analyzers, models, or adapters.  CPU-bound work belongs
in adapters (for example a process-pool wrapper) behind the same
:class:`~trackforge.application.ports.execution.Capability` protocol.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from trackforge.application.dto.artifacts import ArtifactProducer
from trackforge.application.dto.db import (
    RunItemStatus,
    RunRecord,
    RunStatus,
    StepRecord,
    StepStatus,
)
from trackforge.application.ports.execution import (
    Capability,
    StepContext,
    StepInput,
    StepResult,
    StepResultStatus,
    StepWarning,
)
from trackforge.application.ports.repositories import RunRepository
from trackforge.application.ports.storage import ArtifactStore, StorageError
from trackforge.application.services.canonical_json import canonical_json, sha256_hex
from trackforge.domain.errors import RateLimited, TrackForgeError
from trackforge.domain.model._validation import utc_now
from trackforge.domain.model.capabilities import ArtifactType, CapabilityManifest
from trackforge.domain.model.ids import ItemId, RunId, StepId
from trackforge.pipeline.budget import Budget
from trackforge.pipeline.cache import Cache, CacheMiss, cache_key_for_step
from trackforge.pipeline.dag import Node, artifact_key
from trackforge.pipeline.degradation import WarningCode, make_warning
from trackforge.pipeline.errors import (
    InvalidStepResult,
    InvalidTransition,
    PipelineError,
    PlanBlocked,
    RunAlreadyCompleted,
    RunNotResumable,
    UnknownCapability,
)
from trackforge.pipeline.events import Event, EventBus, EventType
from trackforge.pipeline.jobs import JobManager
from trackforge.pipeline.planner import Plan

_TERMINAL_STEP_STATUSES = frozenset(
    {
        StepStatus.SUCCEEDED,
        StepStatus.FAILED,
        StepStatus.SKIPPED,
        StepStatus.CACHED,
    }
)
_SUCCESS_STEP_STATUSES = frozenset({StepStatus.SUCCEEDED, StepStatus.CACHED})


@dataclass(frozen=True, slots=True)
class StepOutcome:
    """Final, user-visible outcome for one plan node."""

    step_id: StepId
    node_id: str
    capability_id: str
    capability_version: str
    status: StepStatus
    attempts: int
    duration_ms: int
    warning_codes: tuple[str, ...] = ()
    error: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class RunResult:
    """The executor's final report for one run."""

    run_id: RunId
    status: RunStatus
    steps: tuple[StepOutcome, ...]
    warnings: tuple[StepWarning, ...]
    artifacts: Mapping[str, StepInput] = field(default_factory=dict)
    error: dict[str, Any] | None = None

    @property
    def completed(self) -> bool:
        return self.status is RunStatus.COMPLETED

    @property
    def failed(self) -> bool:
        return self.status is RunStatus.FAILED

    @property
    def cancelled(self) -> bool:
        return self.status is RunStatus.CANCELLED

    def artifact(self, artifact_type: ArtifactType | str) -> StepInput | None:
        return self.artifacts.get(artifact_key(artifact_type))


@dataclass(frozen=True, slots=True)
class _NodeResult:
    node_id: str
    step_id: StepId
    status: StepStatus
    warnings: tuple[StepWarning, ...] = ()
    duration_ms: int = 0
    error: dict[str, Any] | None = None
    artifacts: tuple[StepInput, ...] = ()
    cached: bool = False
    degraded: bool = False


@dataclass
class _ExecutionState:
    run: RunRecord
    plan: Plan
    item_id: ItemId
    budget: Budget
    cancel_event: asyncio.Event
    forced: set[str]
    force_all: bool
    node_steps: dict[str, StepRecord] = field(default_factory=dict)
    node_status: dict[str, StepStatus] = field(default_factory=dict)
    available: dict[str, StepInput] = field(default_factory=dict)
    outcomes: dict[str, StepOutcome] = field(default_factory=dict)
    warnings: list[StepWarning] = field(default_factory=list)
    degraded: set[str] = field(default_factory=set)

    @property
    def input_keys(self) -> set[str]:
        return {artifact_key(item) for item in self.plan.inputs}

    def remember(self, node_id: str, step: StepRecord) -> None:
        self.node_steps[node_id] = step
        self.node_status[node_id] = step.status


class Executor:
    """Execute plans against a capability registry and persistent state."""

    def __init__(
        self,
        capabilities: (Iterable[Capability] | Mapping[str, Capability]),
        artifact_store: ArtifactStore,
        runs: RunRepository,
        *,
        events: EventBus | None = None,
        cache: Cache | None = None,
        budget: Budget | None = None,
        config: Mapping[str, Any] | None = None,
        max_attempts: int = 3,
        retry_backoff: float = 0.0,
        retry_jitter: float = 0.0,
        io_concurrency: int = 4,
        force_steps: Iterable[str] = (),
        force_all: bool = False,
        clock: Callable[[], datetime] = utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        commit: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if io_concurrency < 1:
            raise ValueError("io_concurrency must be at least 1")
        self._capabilities: dict[str, Capability] = {}
        if isinstance(capabilities, Mapping):
            for key, capability in capabilities.items():
                self._register(str(key), capability)
        else:
            for capability in capabilities:
                self._register(capability.manifest.id, capability)
        self._cache = cache if cache is not None else Cache(artifact_store)
        self._events = events if events is not None else EventBus()
        self._budget_template = budget.clone() if budget is not None else Budget.unlimited()
        self._config = dict(config or {})
        self._max_attempts = max_attempts
        self._retry_backoff = max(0.0, retry_backoff)
        self._retry_jitter = max(0.0, retry_jitter)
        self._io_concurrency = io_concurrency
        self._force_steps = set(force_steps)
        self._force_all = force_all
        self._clock = clock
        self._sleep = sleep
        self._jobs = JobManager(runs, cache=self._cache, clock=clock, commit=commit)
        self._cancel_events: dict[str, asyncio.Event] = {}

    # -- public API ---------------------------------------------------

    @property
    def events(self) -> EventBus:
        return self._events

    @property
    def cache(self) -> Cache:
        return self._cache

    @property
    def jobs(self) -> JobManager:
        return self._jobs

    @property
    def capabilities(self) -> Mapping[str, Capability]:
        return dict(self._capabilities)

    def register(self, capability: Capability, *, key: str | None = None) -> None:
        self._register(key or capability.manifest.id, capability)

    def _register(self, key: str, capability: Capability) -> None:
        if not isinstance(capability, Capability):
            raise TypeError("capability must implement the Capability protocol")
        self._capabilities[key] = capability
        self._capabilities.setdefault(capability.manifest.id, capability)

    def cancel(self, run_id: RunId) -> None:
        """Request cooperative cancellation for a run."""

        self._cancel_events.setdefault(str(run_id), asyncio.Event()).set()

    def is_cancelled(self, run_id: RunId) -> bool:
        event = self._cancel_events.get(str(run_id))
        return event is not None and event.is_set()

    async def run(
        self,
        plan: Plan,
        *,
        item_id: ItemId,
        profile: str = "custom",
        mode: str = "auto",
        run_id: RunId | None = None,
        force_steps: Iterable[str] | None = None,
        force_all: bool | None = None,
        initial_inputs: Mapping[ArtifactType | str, StepInput] | None = None,
    ) -> RunResult:
        """Create a run, persist its steps, and execute the plan."""

        self._validate_plan(plan)
        if run_id is not None:
            existing = await self._jobs.runs.get_run(run_id)
            if existing is not None:
                if existing.status is RunStatus.COMPLETED:
                    raise RunAlreadyCompleted(str(run_id))
                raise PipelineError(
                    "run already exists; use resume to continue it",
                    context={"run_id": str(run_id), "status": existing.status.value},
                )
        run = await self._jobs.create_run(profile, mode, run_id=run_id)
        await self._jobs.ensure_steps(run.id, item_id, plan)
        return await self._execute(
            run,
            plan,
            item_id,
            force_steps=force_steps,
            force_all=force_all,
            initial_inputs=initial_inputs,
        )

    async def execute(
        self,
        plan: Plan,
        *,
        item_id: ItemId,
        profile: str = "custom",
        mode: str = "auto",
        run_id: RunId | None = None,
        force_steps: Iterable[str] | None = None,
        force_all: bool | None = None,
        initial_inputs: Mapping[ArtifactType | str, StepInput] | None = None,
    ) -> RunResult:
        """Alias for :meth:`run`."""

        return await self.run(
            plan,
            item_id=item_id,
            profile=profile,
            mode=mode,
            run_id=run_id,
            force_steps=force_steps,
            force_all=force_all,
            initial_inputs=initial_inputs,
        )

    async def resume(
        self,
        run_id: RunId,
        plan: Plan,
        *,
        item_id: ItemId | None = None,
        force_steps: Iterable[str] | None = None,
        force_all: bool | None = None,
        initial_inputs: Mapping[ArtifactType | str, StepInput] | None = None,
    ) -> RunResult:
        """Recover a running/paused run and continue only unfinished steps."""

        self._validate_plan(plan)
        report = await self._jobs.resume(run_id)
        run = report.run
        if run.status is RunStatus.COMPLETED:
            raise RunAlreadyCompleted(str(run_id))
        if run.status not in (RunStatus.RUNNING, RunStatus.PAUSED):
            raise RunNotResumable(str(run_id), run.status.value)
        if item_id is None:
            if report.steps:
                item_id = report.steps[0].item_id
            else:
                raise ValueError("item_id is required when a run has no step records")
        await self._jobs.start(run_id)
        await self._jobs.ensure_steps(run_id, item_id, plan)
        return await self._execute(
            run,
            plan,
            item_id,
            force_steps=force_steps,
            force_all=force_all,
            initial_inputs=initial_inputs,
            resuming=True,
        )

    # -- validation ---------------------------------------------------

    def _validate_plan(self, plan: Plan) -> None:
        if plan.blocked_reason is not None:
            raise PlanBlocked(plan.blocked_reason)
        for node in plan.nodes:
            capability = self._capabilities.get(node.capability_id)
            if capability is None:
                raise UnknownCapability(node.capability_id, node.capability_version)
            manifest = capability.manifest
            if manifest.version != node.capability_version:
                raise UnknownCapability(node.capability_id, node.capability_version)
            if {artifact_key(item) for item in manifest.provides} != set(node.provides_keys):
                raise PipelineError(
                    "plan node provides differ from the registered capability manifest",
                    context={"node_id": node.node_id, "capability_id": node.capability_id},
                )

    def _capability(self, node: Node) -> Capability:
        capability = self._capabilities.get(node.capability_id)
        if capability is None:
            raise UnknownCapability(node.capability_id, node.capability_version)
        return capability

    # -- execution ----------------------------------------------------

    async def _execute(
        self,
        run: RunRecord,
        plan: Plan,
        item_id: ItemId,
        *,
        force_steps: Iterable[str] | None = None,
        force_all: bool | None = None,
        initial_inputs: Mapping[ArtifactType | str, StepInput] | None = None,
        resuming: bool = False,
    ) -> RunResult:
        run = await self._jobs.start(run.id)
        cancel_event = self._cancel_events.setdefault(str(run.id), asyncio.Event())
        budget = self._budget_template.clone()
        budget.reset()
        forced = set(self._force_steps)
        if force_steps is not None:
            forced = set(force_steps)
        state = _ExecutionState(
            run=run,
            plan=plan,
            item_id=item_id,
            budget=budget,
            cancel_event=cancel_event,
            forced=forced,
            force_all=self._force_all if force_all is None else force_all,
        )
        state.warnings.extend(plan.warnings)
        if initial_inputs:
            for raw_type, value in initial_inputs.items():
                state.available[artifact_key(raw_type)] = value

        steps = await self._jobs.ensure_steps(run.id, item_id, plan)
        await self._bind_steps(state, steps)
        if resuming:
            await self._restore_cached_steps(state)

        self._emit(
            EventType.RUN_STARTED,
            state,
            status="running",
        )

        try:
            await self._schedule(state)
        except asyncio.CancelledError:
            for node in state.plan.ordered_nodes:
                if state.node_status.get(node.node_id) is StepStatus.RUNNING:
                    await self._skip_node(
                        state,
                        node,
                        "run was cancelled while the step was running",
                        WarningCode.CANCELLED,
                    )
            await self._finalize(state, cancelled=True)
            raise
        except Exception as exc:
            state.warnings.append(
                make_warning(
                    WarningCode.CAPABILITY_FAILED,
                    f"executor stopped after an unexpected error: {exc}",
                    retryable=False,
                )
            )
            for node in state.plan.ordered_nodes:
                if state.node_status.get(node.node_id) in (
                    StepStatus.PENDING,
                    StepStatus.READY,
                    StepStatus.RUNNING,
                ):
                    await self._skip_node(
                        state,
                        node,
                        "run aborted before this step could execute",
                        WarningCode.CAPABILITY_FAILED,
                    )

        result = await self._finalize(state)
        return result

    async def _bind_steps(
        self,
        state: _ExecutionState,
        steps: Iterable[StepRecord],
    ) -> None:
        by_capability = {(step.capability_id, step.capability_version): step for step in steps}
        for node in state.plan.ordered_nodes:
            step = by_capability.get((node.capability_id, node.capability_version))
            if step is None:
                raise PipelineError(
                    "step record missing for plan node",
                    context={"node_id": node.node_id},
                )
            state.node_steps[node.node_id] = step
            if step.status in _TERMINAL_STEP_STATUSES:
                state.node_status[node.node_id] = step.status
            else:
                state.node_status[node.node_id] = StepStatus.PENDING
                if step.status is not StepStatus.PENDING:
                    step = await self._jobs.reset_step(step.id)
                    state.node_steps[node.node_id] = step

    async def _restore_cached_steps(self, state: _ExecutionState) -> None:
        """Rehydrate artifacts from steps that finished before a crash."""

        for node in state.plan.ordered_nodes:
            step = state.node_steps[node.node_id]
            if step.status not in _SUCCESS_STEP_STATUSES:
                continue
            restored = False
            if step.cache_key is not None:
                try:
                    capability = self._capability(node)
                    expected = {
                        str(item): item.schema_version
                        for item in capability.manifest.provides
                        if isinstance(item, ArtifactType)
                    }
                    ref = await self._cache.lookup(
                        step.cache_key,
                        expected or None,
                    )
                    if ref is not None:
                        entry = await self._cache.load(ref, expected or None)
                        for artifact in entry.artifacts:
                            self._register_input(state, artifact.to_step_input())
                        if entry.result.status is StepResultStatus.DEGRADED:
                            state.degraded.update(node.provides_keys)
                        else:
                            state.degraded.difference_update(node.provides_keys)
                        state.node_status[node.node_id] = step.status
                        state.outcomes[node.node_id] = self._outcome(
                            state, node, step, duration_ms=0
                        )
                        restored = True
                except (CacheMiss, StorageError, PipelineError):
                    restored = False
            if not restored:
                step = await self._jobs.reset_step(
                    step.id,
                    reason="cached artifact missing; step will be recomputed",
                )
                state.node_steps[node.node_id] = step
                state.node_status[node.node_id] = StepStatus.PENDING

    async def _schedule(self, state: _ExecutionState) -> None:
        semaphore = asyncio.Semaphore(self._io_concurrency)
        while True:
            if state.cancel_event.is_set():
                return
            ready: list[Node] = []
            for node in state.plan.ordered_nodes:
                status = state.node_status.get(node.node_id, StepStatus.PENDING)
                if status is not StepStatus.PENDING:
                    continue
                decision = self._node_decision(state, node)
                if decision == "wait":
                    continue
                if decision == "skip":
                    await self._skip_node(
                        state,
                        node,
                        "output already provided by an earlier step",
                        WarningCode.SKIPPED,
                    )
                    continue
                if decision == "blocked":
                    await self._skip_node(
                        state,
                        node,
                        f"required inputs for {node.node_id} could not be produced",
                        WarningCode.NO_DATA,
                    )
                    continue
                ready.append(node)

            if not ready:
                # Any remaining pending node is unreachable; make the
                # degradation visible instead of spinning forever.
                for node in state.plan.ordered_nodes:
                    if state.node_status.get(node.node_id) is StepStatus.PENDING:
                        await self._skip_node(
                            state,
                            node,
                            "step could not be scheduled because a dependency failed",
                            WarningCode.NO_DATA,
                        )
                return

            async def guarded(node: Node) -> _NodeResult:
                async with semaphore:
                    return await self._execute_node(state, node)

            results = await asyncio.gather(*(guarded(node) for node in ready))
            for result in results:
                self._apply_result(state, result)
            if state.cancel_event.is_set():
                return

    def _node_decision(
        self,
        state: _ExecutionState,
        node: Node,
    ) -> Literal["run", "wait", "skip", "blocked"]:
        provides = node.provides_keys
        if provides and all(
            key in state.available and key not in state.degraded for key in provides
        ):
            return "skip"
        if not self._fallback_gate(state, node):
            return "wait"
        dependency = self._dependency_state(state, node)
        return dependency

    def _fallback_gate(self, state: _ExecutionState, node: Node) -> bool:
        for key in node.provides_keys:
            if key in state.available and key not in state.degraded:
                continue
            providers = state.plan.provider_groups.get(key, ())
            if node.node_id not in providers:
                continue
            index = providers.index(node.node_id)
            for earlier in providers[:index]:
                status = state.node_status.get(earlier)
                if status is None or status not in _TERMINAL_STEP_STATUSES:
                    return False
        return True

    def _dependency_state(
        self,
        state: _ExecutionState,
        node: Node,
    ) -> Literal["run", "wait", "blocked"]:
        input_keys = state.input_keys
        for requirement in node.requires:
            key = artifact_key(requirement)
            if key in state.available or key in input_keys:
                continue
            providers = state.plan.provider_groups.get(key)
            if not providers:
                return "blocked"
            statuses = [
                state.node_status.get(provider, StepStatus.PENDING) for provider in providers
            ]
            if any(
                status in (StepStatus.PENDING, StepStatus.READY, StepStatus.RUNNING)
                for status in statuses
            ):
                return "wait"
            return "blocked"
        return "run"

    async def _execute_node(
        self,
        state: _ExecutionState,
        node: Node,
    ) -> _NodeResult:
        started = time.monotonic()
        step = state.node_steps[node.node_id]
        capability = self._capability(node)
        manifest = capability.manifest
        inputs = self._inputs_for(state, node)
        cacheable = bool(node.cacheable and manifest.cacheable)

        cache_key: str | None = None
        try:
            cache_key = cache_key_for_step(
                inputs,
                manifest,
                params=node.params,
                config=self._config,
            )
            await self._jobs.set_step_cache_key(step.id, cache_key)
            step = self._jobs.step(step.id)
            state.node_steps[node.node_id] = step
        except Exception as exc:  # a cache-key failure must not kill the run
            state.warnings.append(
                make_warning(
                    WarningCode.INVALID_OUTPUT,
                    f"could not compute cache key for {node.node_id}: {exc}",
                    capability_id=node.capability_id,
                )
            )

        if cacheable and cache_key is not None and not self._is_forced(state, node):
            expected = {
                str(item): item.schema_version
                for item in manifest.provides
                if isinstance(item, ArtifactType)
            }
            try:
                ref = await self._cache.lookup(cache_key, expected or None)
                if ref is not None:
                    entry = await self._cache.load(ref, expected or None)
                    cache_warnings = list(entry.result.warnings)
                    await self._jobs.step_cached(
                        step.id,
                        cache_key=cache_key,
                        warnings=cache_warnings,
                    )
                    step = self._jobs.step(step.id)
                    state.node_steps[node.node_id] = step
                    state.node_status[node.node_id] = StepStatus.CACHED
                    self._emit(
                        EventType.STEP_CACHED,
                        state,
                        node=node,
                        step=step,
                        duration_ms=0,
                        status="cached",
                        warning_codes=[warning.code for warning in cache_warnings],
                    )
                    return _NodeResult(
                        node_id=node.node_id,
                        step_id=step.id,
                        status=StepStatus.CACHED,
                        warnings=tuple(cache_warnings),
                        duration_ms=int((time.monotonic() - started) * 1000),
                        artifacts=tuple(artifact.to_step_input() for artifact in entry.artifacts),
                        cached=True,
                    )
            except (CacheMiss, StorageError, PipelineError, ValueError):
                pass

        if state.cancel_event.is_set():
            return await self._cancel_node(state, node, step, started)
        if not state.budget.can_afford(node.cost):
            warning = make_warning(
                WarningCode.BUDGET_EXCEEDED,
                f"step {node.node_id} was skipped because the remaining budget is insufficient",
                capability_id=node.capability_id,
                user_action="Increase the profile budget or disable optional analysis.",
            )
            await self._skip_node(state, node, warning.message, WarningCode.BUDGET_EXCEEDED)
            return _NodeResult(
                node_id=node.node_id,
                step_id=step.id,
                status=StepStatus.SKIPPED,
                warnings=(warning,),
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        if step.status is StepStatus.PENDING:
            step = await self._jobs.step_ready(step.id)
            state.node_steps[node.node_id] = step
        step = await self._jobs.step_started(step.id)
        state.node_steps[node.node_id] = step
        state.node_status[node.node_id] = StepStatus.RUNNING
        self._emit(
            EventType.STEP_STARTED,
            state,
            node=node,
            step=step,
            status="running",
        )

        warnings: list[StepWarning] = []
        attempt = 1
        while True:
            if state.cancel_event.is_set():
                return await self._cancel_node(state, node, step, started, warnings)
            context = StepContext(
                run_id=state.run.id,
                item_id=state.item_id,
                step_id=step.id,
                inputs=inputs,
                params=node.params,
                config_subset=self._config_subset(manifest),
                budget=state.budget.snapshot(),
                force=self._is_forced(state, node),
                cancelled=state.cancel_event,
                report_progress=self._progress_callback(state, node, step),
            )
            try:
                result = await capability.run(context)
                self._validate_result(node, manifest, result)
            except asyncio.CancelledError:
                if state.cancel_event.is_set():
                    return await self._cancel_node(state, node, step, started, warnings)
                raise
            except Exception as exc:
                retryable = self._retryable(exc)
                warning = self._failure_warning(node, exc)
                warnings.append(warning)
                if retryable and attempt < self._max_attempts:
                    emit_codes = [warning.code]
                    self._emit(
                        EventType.STEP_PROGRESS,
                        state,
                        node=node,
                        step=step,
                        status="retrying",
                        warning_codes=emit_codes,
                        message=f"attempt {attempt} failed; retrying",
                    )
                    await self._backoff(attempt)
                    attempt += 1
                    if state.cancel_event.is_set():
                        return await self._cancel_node(state, node, step, started, warnings)
                    step = await self._jobs.step_started(step.id)
                    state.node_steps[node.node_id] = step
                    continue
                if retryable:
                    warnings.append(
                        make_warning(
                            WarningCode.RETRY_EXHAUSTED,
                            f"retries exhausted after {attempt} attempts",
                            capability_id=node.capability_id,
                            retryable=True,
                        )
                    )
                await self._jobs.step_failed(
                    step.id,
                    error=exc,
                    warnings=warnings,
                    cache_key=cache_key,
                )
                failed = self._jobs.step(step.id)
                state.node_steps[node.node_id] = failed
                state.node_status[node.node_id] = StepStatus.FAILED
                state.warnings.extend(warnings)
                self._emit(
                    EventType.STEP_FAILED,
                    state,
                    node=node,
                    step=failed,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    status="failed",
                    warning_codes=[item.code for item in warnings],
                    message="step failed",
                )
                return _NodeResult(
                    node_id=node.node_id,
                    step_id=step.id,
                    status=StepStatus.FAILED,
                    warnings=tuple(warnings),
                    duration_ms=int((time.monotonic() - started) * 1000),
                    error=_error_payload(exc),
                )

            if result.status is StepResultStatus.SKIPPED:
                warnings.extend(result.warnings)
                if not warnings:
                    warnings.append(
                        make_warning(
                            WarningCode.SKIPPED,
                            f"{node.capability_id} reported that it was skipped",
                            capability_id=node.capability_id,
                        )
                    )
                await self._jobs.step_skipped(
                    step.id,
                    warnings=warnings,
                    cache_key=cache_key,
                )
                skipped = self._jobs.step(step.id)
                state.node_steps[node.node_id] = skipped
                state.node_status[node.node_id] = StepStatus.SKIPPED
                state.warnings.extend(warnings)
                self._emit(
                    EventType.STEP_SKIPPED,
                    state,
                    node=node,
                    step=skipped,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    status="skipped",
                    warning_codes=[item.code for item in warnings],
                )
                return _NodeResult(
                    node_id=node.node_id,
                    step_id=step.id,
                    status=StepStatus.SKIPPED,
                    warnings=tuple(warnings),
                    duration_ms=int((time.monotonic() - started) * 1000),
                )

            warnings.extend(result.warnings)
            present = {str(artifact.type) for artifact in result.artifacts}
            for provided in manifest.provides:
                key = artifact_key(provided)
                if key not in present and key not in state.available:
                    warnings.append(
                        make_warning(
                            WarningCode.NO_DATA,
                            f"{node.capability_id} did not return {key}",
                            capability_id=node.capability_id,
                        )
                    )
            if result.status is StepResultStatus.DEGRADED and not result.warnings:
                warnings.append(
                    make_warning(
                        WarningCode.DEGRADED,
                        f"{node.capability_id} completed in degraded mode",
                        capability_id=node.capability_id,
                    )
                )

            refs: list[Any] = []
            if cacheable and result.artifacts and cache_key is not None:
                try:
                    refs = await self._cache.store(
                        cache_key,
                        result,
                        producer=ArtifactProducer(
                            id=manifest.id,
                            version=manifest.version,
                        ),
                        params=node.params,
                        inputs=inputs,
                    )
                except (StorageError, PipelineError, OSError, ValueError) as exc:
                    warnings.append(
                        make_warning(
                            WarningCode.CACHE_WRITE_FAILED,
                            f"step succeeded but its result could not be cached: {exc}",
                            capability_id=node.capability_id,
                            retryable=True,
                        )
                    )
            state.budget.consume(result.actual_cost)
            step_inputs = self._register_artifacts(
                state,
                node,
                result,
                refs=refs,
                fallback_cache_key=cache_key,
            )
            await self._jobs.step_succeeded(
                step.id,
                cache_key=cache_key,
                warnings=warnings,
            )
            succeeded = self._jobs.step(step.id)
            state.node_steps[node.node_id] = succeeded
            state.node_status[node.node_id] = StepStatus.SUCCEEDED
            state.warnings.extend(warnings)
            is_degraded = result.status is StepResultStatus.DEGRADED or any(
                warning.code == WarningCode.NO_DATA.value for warning in warnings
            )
            event_status = "degraded" if is_degraded else "succeeded"
            self._emit(
                EventType.STEP_FINISHED,
                state,
                node=node,
                step=succeeded,
                duration_ms=int((time.monotonic() - started) * 1000),
                status=event_status,
                warning_codes=[warning.code for warning in warnings],
            )
            return _NodeResult(
                node_id=node.node_id,
                step_id=step.id,
                status=StepStatus.SUCCEEDED,
                warnings=tuple(warnings),
                duration_ms=int((time.monotonic() - started) * 1000),
                artifacts=step_inputs,
                degraded=is_degraded,
            )

    async def _cancel_node(
        self,
        state: _ExecutionState,
        node: Node,
        step: StepRecord,
        started: float,
        warnings: list[StepWarning] | None = None,
    ) -> _NodeResult:
        payload = list(warnings or [])
        payload.append(
            make_warning(
                WarningCode.CANCELLED,
                f"step {node.node_id} was cancelled",
                capability_id=node.capability_id,
                retryable=False,
            )
        )
        current = self._jobs.step(step.id)
        if current.status in (StepStatus.PENDING, StepStatus.READY, StepStatus.RUNNING):
            await self._jobs.step_skipped(step.id, warnings=payload)
            current = self._jobs.step(step.id)
        state.node_steps[node.node_id] = current
        state.node_status[node.node_id] = current.status
        state.warnings.extend(payload)
        self._emit(
            EventType.STEP_SKIPPED,
            state,
            node=node,
            step=current,
            duration_ms=int((time.monotonic() - started) * 1000),
            status="cancelled",
            warning_codes=[warning.code for warning in payload],
        )
        return _NodeResult(
            node_id=node.node_id,
            step_id=step.id,
            status=current.status,
            warnings=tuple(payload),
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    async def _skip_node(
        self,
        state: _ExecutionState,
        node: Node,
        message: str,
        code: WarningCode | str,
    ) -> None:
        step = state.node_steps[node.node_id]
        current = self._jobs.step(step.id)
        warning = make_warning(
            code,
            message,
            capability_id=node.capability_id,
        )
        if current.status in (StepStatus.PENDING, StepStatus.READY, StepStatus.RUNNING):
            current = await self._jobs.step_skipped(
                step.id,
                warnings=(warning,),
                cache_key=current.cache_key,
            )
            state.warnings.append(warning)
            self._emit(
                EventType.STEP_SKIPPED,
                state,
                node=node,
                step=current,
                status="skipped",
                warning_codes=[warning.code],
                message=message,
            )
        state.node_steps[node.node_id] = current
        state.node_status[node.node_id] = current.status
        state.outcomes[node.node_id] = self._outcome(state, node, current, duration_ms=0)

    def _apply_result(self, state: _ExecutionState, result: _NodeResult) -> None:
        node = state.plan.node(result.node_id)
        state.node_status[node.node_id] = result.status
        for artifact in result.artifacts:
            self._register_input(state, artifact)
        if result.status is StepStatus.SUCCEEDED:
            if result.degraded:
                state.degraded.update(node.provides_keys)
            else:
                state.degraded.difference_update(node.provides_keys)
        state.outcomes[node.node_id] = StepOutcome(
            step_id=result.step_id,
            node_id=result.node_id,
            capability_id=node.capability_id,
            capability_version=node.capability_version,
            status=result.status,
            attempts=state.node_steps[node.node_id].attempts,
            duration_ms=result.duration_ms,
            warning_codes=tuple(warning.code for warning in result.warnings),
            error=result.error,
        )

    async def _finalize(
        self,
        state: _ExecutionState,
        *,
        cancelled: bool = False,
    ) -> RunResult:
        """Persist the run's terminal state and emit exactly one terminal event."""

        missing_required = [
            artifact_key(item)
            for item in state.plan.required_outputs
            if artifact_key(item) not in state.available
            and artifact_key(item) not in state.input_keys
        ]
        for item in state.plan.desired_outputs:
            key = artifact_key(item)
            if (
                key not in state.available
                and key not in state.input_keys
                and key not in {artifact_key(required) for required in state.plan.required_outputs}
            ):
                state.warnings.append(
                    make_warning(
                        WarningCode.NO_DATA,
                        f"desired output {key} is unavailable; it remains unknown",
                    )
                )

        if cancelled or state.cancel_event.is_set():
            target = RunStatus.CANCELLED
        elif missing_required:
            target = RunStatus.FAILED
        else:
            target = RunStatus.COMPLETED

        error: dict[str, Any] | None = None
        if target is RunStatus.FAILED:
            error = {
                "type": "RequiredOutputMissing",
                "message": "required outputs were not produced",
                "required_outputs": missing_required,
            }

        try:
            if target is RunStatus.CANCELLED:
                persisted = await self._jobs.cancel(state.run.id)
            elif target is RunStatus.FAILED:
                persisted = await self._jobs.fail(state.run.id, error)
            else:
                persisted = await self._jobs.complete(state.run.id)
        except (InvalidTransition, RunNotResumable):
            persisted = await self._jobs.get_run(state.run.id)
            if persisted.status is not target:
                target = persisted.status
                error = persisted.error

        item_status = {
            RunStatus.COMPLETED: RunItemStatus.SUCCEEDED,
            RunStatus.FAILED: RunItemStatus.FAILED,
            RunStatus.CANCELLED: RunItemStatus.CANCELLED,
        }.get(persisted.status)
        if item_status is not None:
            try:
                await self._jobs.set_item_status(
                    state.run.id,
                    state.item_id,
                    item_status,
                )
            except (PipelineError, LookupError):
                pass

        outcomes = self._outcomes(state)
        all_warnings = list(state.warnings)
        for outcome in outcomes:
            all_warnings.extend(self._outcome_warnings(state, outcome))
        warnings = _dedupe_warnings(all_warnings)
        result = RunResult(
            run_id=persisted.id,
            status=persisted.status,
            steps=outcomes,
            warnings=warnings,
            artifacts=dict(state.available),
            error=error if persisted.error is None else persisted.error,
        )
        self._emit_terminal(state, result)
        return result

    def _outcomes(self, state: _ExecutionState) -> tuple[StepOutcome, ...]:
        outcomes: list[StepOutcome] = []
        for node in state.plan.ordered_nodes:
            existing = state.outcomes.get(node.node_id)
            if existing is not None:
                outcomes.append(existing)
                continue
            step = state.node_steps.get(node.node_id)
            if step is None:
                continue
            outcomes.append(self._outcome(state, node, step, duration_ms=0))
        return tuple(outcomes)

    def _outcome(
        self,
        state: _ExecutionState,
        node: Node,
        step: StepRecord,
        *,
        duration_ms: int,
    ) -> StepOutcome:
        warning_codes = tuple(
            str(warning.get("code", "unknown")) if isinstance(warning, Mapping) else str(warning)
            for warning in step.warnings
        )
        return StepOutcome(
            step_id=step.id,
            node_id=node.node_id,
            capability_id=node.capability_id,
            capability_version=node.capability_version,
            status=step.status,
            attempts=step.attempts,
            duration_ms=duration_ms,
            warning_codes=warning_codes,
            error=step.error,
        )

    def _outcome_warnings(
        self,
        state: _ExecutionState,
        outcome: StepOutcome,
    ) -> tuple[StepWarning, ...]:
        step = state.node_steps.get(outcome.node_id)
        if step is None:
            return ()
        warnings: list[StepWarning] = []
        for warning in step.warnings:
            if isinstance(warning, Mapping):
                code = str(warning.get("code", "unknown"))
                message = str(warning.get("message", code))
                warnings.append(
                    StepWarning(
                        code=code,
                        message=message,
                        capability_id=outcome.capability_id,
                        provider=(
                            None
                            if warning.get("provider") is None
                            else str(warning.get("provider"))
                        ),
                        retryable=bool(warning.get("retryable", False)),
                    )
                )
            else:
                warnings.append(
                    StepWarning(
                        code=str(warning),
                        message=str(warning),
                        capability_id=outcome.capability_id,
                    )
                )
        return tuple(warnings)

    # -- helpers ------------------------------------------------------

    def _inputs_for(
        self,
        state: _ExecutionState,
        node: Node,
    ) -> dict[ArtifactType, StepInput]:
        inputs: dict[ArtifactType, StepInput] = {}
        for requirement in node.requires:
            key = artifact_key(requirement)
            value = state.available.get(key)
            if value is not None:
                inputs[value.type] = value
        return inputs

    def _register_artifacts(
        self,
        state: _ExecutionState,
        node: Node,
        result: StepResult,
        *,
        refs: list[Any],
        fallback_cache_key: str | None,
    ) -> tuple[StepInput, ...]:
        registered: list[StepInput] = []
        for index, artifact in enumerate(result.artifacts):
            if artifact.payload_format == "json":
                payload_bytes = canonical_json(artifact.payload)
            else:
                payload_bytes = bytes(artifact.payload)
            ref = refs[index] if index < len(refs) else None
            if ref is not None:
                cache_key = str(ref.cache_key)
                payload_hash = str(ref.payload_hash)
            else:
                cache_key = fallback_cache_key or ""
                payload_hash = sha256_hex(payload_bytes)
            step_input = StepInput(
                type=artifact.type,
                artifact_id=artifact.artifact_id,
                schema_version=artifact.type.schema_version,
                cache_key=cache_key,
                payload_hash=payload_hash,
                payload=payload_bytes,
            )
            registered.append(step_input)
            self._register_input(state, step_input)
        return tuple(registered)

    def _register_input(self, state: _ExecutionState, artifact: StepInput) -> None:
        state.available[str(artifact.type)] = artifact

    def _validate_result(
        self,
        node: Node,
        manifest: CapabilityManifest,
        result: StepResult,
    ) -> None:
        if not isinstance(result, StepResult):
            raise InvalidStepResult(
                "capability returned a value that is not a StepResult",
                retryable=False,
            )
        allowed = set(node.provides_keys)
        seen: set[str] = set()
        for artifact in result.artifacts:
            key = str(artifact.type)
            if key not in allowed:
                raise InvalidStepResult(
                    "capability returned an artifact it does not declare in provides",
                    context={"artifact_type": key, "provides": sorted(allowed)},
                    retryable=False,
                )
            if key in seen:
                raise InvalidStepResult(
                    "capability returned duplicate artifacts of the same type",
                    context={"artifact_type": key},
                    retryable=False,
                )
            seen.add(key)
        if result.status is StepResultStatus.SKIPPED and result.artifacts:
            raise InvalidStepResult(
                "a skipped capability may not return artifacts",
                retryable=False,
            )
        _ = manifest

    def _config_subset(self, manifest: CapabilityManifest) -> dict[str, Any]:
        from trackforge.pipeline.cache import relevant_config

        return relevant_config(manifest, self._config)

    def _is_forced(self, state: _ExecutionState, node: Node) -> bool:
        return (
            state.force_all
            or node.node_id in state.forced
            or node.capability_id in state.forced
            or str(state.node_steps[node.node_id].id) in state.forced
        )

    def _retryable(self, error: BaseException) -> bool:
        if isinstance(error, TrackForgeError):
            return error.retryable
        return bool(getattr(error, "retryable", False))

    def _failure_warning(self, node: Node, error: BaseException) -> StepWarning:
        if isinstance(error, RateLimited):
            code = WarningCode.RATE_LIMITED
        elif isinstance(error, InvalidStepResult):
            code = WarningCode.INVALID_OUTPUT
        elif isinstance(error, TrackForgeError):
            code = WarningCode.PROVIDER_UNAVAILABLE
        else:
            code = WarningCode.CAPABILITY_FAILED
        return make_warning(
            code,
            str(error) or type(error).__name__,
            capability_id=node.capability_id,
            provider=node.capability_id,
            retryable=self._retryable(error),
            user_action=getattr(error, "user_action", None),
        )

    async def _backoff(self, attempt: int) -> None:
        delay = self._retry_backoff * (2 ** (attempt - 1))
        if self._retry_jitter > 0:
            milliseconds = max(1, int(self._retry_jitter * 1000))
            delay += secrets.randbelow(milliseconds + 1) / 1000.0
        if delay > 0:
            await self._sleep(min(delay, 60.0))

    def _progress_callback(
        self,
        state: _ExecutionState,
        node: Node,
        step: StepRecord,
    ) -> Callable[[float, str | None], None]:
        def report(fraction: float, message: str | None = None) -> None:
            self._emit(
                EventType.STEP_PROGRESS,
                state,
                node=node,
                step=step,
                status="running",
                message=message,
                progress=fraction,
            )

        return report

    def _emit(
        self,
        event_type: EventType,
        state: _ExecutionState,
        *,
        node: Node | None = None,
        step: StepRecord | None = None,
        duration_ms: int | None = None,
        status: str | None = None,
        warning_codes: Iterable[str] = (),
        message: str | None = None,
        progress: float | None = None,
    ) -> Event:
        codes = tuple(warning_codes)
        if progress is not None:
            message = f"{progress:.0%} {message}" if message else f"{progress:.0%}"
        return self._events.publish(
            Event(
                type=event_type,
                run_id=state.run.id,
                item_id=state.item_id,
                step_id=None if step is None else step.id,
                capability=None if node is None else node.capability_id,
                provider=None if node is None else node.capability_id,
                duration_ms=duration_ms,
                status=status,
                warning_codes=codes,
                message=message,
            )
        )

    def _emit_terminal(self, state: _ExecutionState, result: RunResult) -> None:
        if result.status is RunStatus.CANCELLED:
            event_type = EventType.RUN_CANCELLED
            status = "cancelled"
        elif result.status is RunStatus.FAILED:
            event_type = EventType.RUN_FAILED
            status = "failed"
        elif result.status is RunStatus.COMPLETED:
            event_type = EventType.RUN_FINISHED
            status = "completed"
        else:  # pragma: no cover - defensive: executor only persists terminals
            return
        self._events.publish(
            Event(
                type=event_type,
                run_id=state.run.id,
                item_id=state.item_id,
                status=status,
                warning_codes=tuple(warning.code for warning in result.warnings),
            )
        )


def _dedupe_warnings(warnings: Iterable[StepWarning]) -> tuple[StepWarning, ...]:
    result: list[StepWarning] = []
    seen: set[tuple[str, str, str | None]] = set()
    for warning in warnings:
        key = (warning.code, warning.message, warning.capability_id)
        if key not in seen:
            seen.add(key)
            result.append(warning)
    return tuple(result)


def _error_payload(error: BaseException) -> dict[str, Any]:
    if hasattr(error, "to_dict"):
        payload = error.to_dict()
        if isinstance(payload, dict):
            return payload
    return {
        "type": type(error).__name__,
        "message": str(error),
        "retryable": bool(getattr(error, "retryable", False)),
    }


__all__ = ["Executor", "RunResult", "StepOutcome"]
