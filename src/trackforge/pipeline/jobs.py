"""Run/step state machine, durable checkpoints, and resume recovery.

The executor owns resource management and scheduling; this module owns legal
transitions and persistence through :class:`RunRepository`.  Every mutation is
checkpointed immediately (optionally committed through a caller-supplied
callback) so a crash between steps never loses a successful step.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from trackforge.application.dto.db import (
    RunItemStatus,
    RunRecord,
    RunStatus,
    StepRecord,
    StepStatus,
)
from trackforge.application.ports.execution import StepWarning
from trackforge.application.ports.repositories import RunRepository
from trackforge.domain.errors import DuplicateRecord
from trackforge.domain.model._validation import utc_now
from trackforge.domain.model.ids import ItemId, RunId, StepId
from trackforge.pipeline.cache import Cache
from trackforge.pipeline.errors import (
    DuplicateNodeError,
    InvalidTransition,
    RunAlreadyCompleted,
    RunNotFound,
    RunNotResumable,
    StepNotFound,
)
from trackforge.pipeline.planner import Plan

_RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.PLANNED: frozenset({RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED}),
    RunStatus.RUNNING: frozenset(
        {RunStatus.PAUSED, RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.PAUSED: frozenset(
        {RunStatus.RUNNING, RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}

_STEP_TRANSITIONS: dict[StepStatus, frozenset[StepStatus]] = {
    StepStatus.PENDING: frozenset({StepStatus.READY, StepStatus.SKIPPED}),
    StepStatus.READY: frozenset({StepStatus.RUNNING, StepStatus.PENDING, StepStatus.SKIPPED}),
    StepStatus.RUNNING: frozenset({StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.SKIPPED}),
    StepStatus.SUCCEEDED: frozenset(),
    StepStatus.FAILED: frozenset(),
    StepStatus.SKIPPED: frozenset(),
    StepStatus.CACHED: frozenset(),
}

_STEP_RECOVERY_TRANSITIONS: dict[StepStatus, frozenset[StepStatus]] = {
    StepStatus.RUNNING: frozenset({StepStatus.PENDING, StepStatus.CACHED, StepStatus.SUCCEEDED}),
    StepStatus.READY: frozenset({StepStatus.PENDING, StepStatus.CACHED}),
    StepStatus.PENDING: frozenset({StepStatus.CACHED}),
    StepStatus.SUCCEEDED: frozenset({StepStatus.PENDING}),
    StepStatus.CACHED: frozenset({StepStatus.PENDING}),
}


class RunStateMachine:
    """Legal run lifecycle transitions."""

    @staticmethod
    def can_transition(current: RunStatus | str, target: RunStatus | str) -> bool:
        source = RunStatus(current)
        destination = RunStatus(target)
        if source is destination:
            return True
        return destination in _RUN_TRANSITIONS[source]

    @staticmethod
    def validate(current: RunStatus | str, target: RunStatus | str) -> None:
        if not RunStateMachine.can_transition(current, target):
            raise InvalidTransition("run", str(current), str(target))

    @staticmethod
    def terminal(status: RunStatus | str) -> bool:
        return not _RUN_TRANSITIONS[RunStatus(status)]


class StepStateMachine:
    """Legal step lifecycle transitions, including crash recovery."""

    @staticmethod
    def can_transition(
        current: StepStatus | str,
        target: StepStatus | str,
        *,
        recovering: bool = False,
    ) -> bool:
        source = StepStatus(current)
        destination = StepStatus(target)
        if source is destination:
            return True
        if destination in _STEP_TRANSITIONS[source]:
            return True
        if recovering:
            return destination in _STEP_RECOVERY_TRANSITIONS.get(source, frozenset())
        return False

    @staticmethod
    def validate(
        current: StepStatus | str,
        target: StepStatus | str,
        *,
        recovering: bool = False,
    ) -> None:
        if not StepStateMachine.can_transition(current, target, recovering=recovering):
            raise InvalidTransition("step", str(current), str(target))

    @staticmethod
    def terminal(status: StepStatus | str) -> bool:
        return StepStatus(status) in {
            StepStatus.SUCCEEDED,
            StepStatus.FAILED,
            StepStatus.SKIPPED,
            StepStatus.CACHED,
        }


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """Result of checking a resumable run's steps and artifacts."""

    run: RunRecord
    steps: tuple[StepRecord, ...]
    cached: tuple[StepId, ...] = ()
    pending: tuple[StepId, ...] = ()

    @property
    def resumed(self) -> bool:
        return self.run.status in (RunStatus.RUNNING, RunStatus.PAUSED)


def _warning_payload(
    warnings: Iterable[StepWarning | Mapping[str, Any] | str] | None,
) -> tuple[dict[str, Any] | str, ...]:
    if warnings is None:
        return ()
    result: list[dict[str, Any] | str] = []
    for warning in warnings:
        if isinstance(warning, StepWarning):
            result.append(warning.to_dict())
        elif isinstance(warning, Mapping):
            result.append(dict(warning))
        elif isinstance(warning, str):
            result.append(warning)
        else:
            raise TypeError("warnings must contain StepWarning, mapping, or string")
    return tuple(result)


def _error_payload(error: BaseException | Mapping[str, Any] | None) -> dict[str, Any] | None:
    if error is None:
        return None
    if isinstance(error, Mapping):
        return dict(error)
    if hasattr(error, "to_dict"):
        value = error.to_dict()
        if isinstance(value, dict):
            return value
    return {
        "type": type(error).__name__,
        "message": str(error),
        "retryable": bool(getattr(error, "retryable", False)),
    }


class JobManager:
    """Persist checkpointed run/step state decisions."""

    def __init__(
        self,
        runs: RunRepository,
        *,
        cache: Cache | None = None,
        clock: Callable[[], datetime] = utc_now,
        commit: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._runs = runs
        self._cache = cache
        self._clock = clock
        self._commit_callback = commit
        self._steps: dict[str, StepRecord] = {}
        self._runs_by_id: dict[str, RunRecord] = {}

    @property
    def runs(self) -> RunRepository:
        return self._runs

    async def _commit(self) -> None:
        if self._commit_callback is not None:
            await self._commit_callback()

    async def get_run(self, run_id: RunId) -> RunRecord:
        run = await self._runs.get_run(run_id)
        if run is None:
            raise RunNotFound(str(run_id))
        self._runs_by_id[str(run.id)] = run
        return run

    async def create_run(
        self,
        profile: str,
        mode: str,
        *,
        run_id: RunId | None = None,
        status: RunStatus | str = RunStatus.PLANNED,
        started_at: datetime | None = None,
        config_snapshot_hash: str | None = None,
    ) -> RunRecord:
        record = await self._runs.create_run(
            profile,
            mode,
            status=status,
            run_id=run_id,
            started_at=started_at,
            config_snapshot_hash=config_snapshot_hash,
        )
        self._runs_by_id[str(record.id)] = record
        await self._commit()
        return record

    async def start(self, run_id: RunId) -> RunRecord:
        run = await self.get_run(run_id)
        if run.status is RunStatus.RUNNING:
            return run
        if run.status is RunStatus.COMPLETED:
            raise RunAlreadyCompleted(str(run_id))
        if run.status not in (RunStatus.PLANNED, RunStatus.PAUSED):
            raise RunNotResumable(str(run_id), run.status.value)
        RunStateMachine.validate(run.status, RunStatus.RUNNING)
        updated = await self._runs.update_run(
            run_id,
            status=RunStatus.RUNNING,
            started_at=run.started_at or self._clock(),
        )
        self._runs_by_id[str(run_id)] = updated
        await self._commit()
        return updated

    async def pause(self, run_id: RunId) -> RunRecord:
        run = await self.get_run(run_id)
        RunStateMachine.validate(run.status, RunStatus.PAUSED)
        updated = await self._runs.update_run(run_id, status=RunStatus.PAUSED)
        self._runs_by_id[str(run_id)] = updated
        await self._commit()
        return updated

    async def finish(
        self,
        run_id: RunId,
        status: RunStatus | str,
        *,
        error: BaseException | Mapping[str, Any] | None = None,
    ) -> RunRecord:
        target = RunStatus(status)
        run = await self.get_run(run_id)
        if run.status is target:
            return run
        if RunStateMachine.terminal(run.status):
            raise RunNotResumable(str(run_id), run.status.value)
        RunStateMachine.validate(run.status, target)
        updated = await self._runs.update_run(
            run_id,
            status=target,
            finished_at=self._clock(),
            error=_error_payload(error),
        )
        self._runs_by_id[str(run_id)] = updated
        await self._commit()
        return updated

    async def complete(self, run_id: RunId) -> RunRecord:
        return await self.finish(run_id, RunStatus.COMPLETED)

    async def fail(
        self,
        run_id: RunId,
        error: BaseException | Mapping[str, Any] | None = None,
    ) -> RunRecord:
        return await self.finish(run_id, RunStatus.FAILED, error=error)

    async def cancel(self, run_id: RunId) -> RunRecord:
        return await self.finish(run_id, RunStatus.CANCELLED)

    # -- steps --------------------------------------------------------

    async def ensure_steps(
        self,
        run_id: RunId,
        item_id: ItemId,
        plan: Plan,
    ) -> tuple[StepRecord, ...]:
        """Create missing steps for a plan and return them in node order."""

        try:
            await self._runs.add_item(run_id, item_id)
        except DuplicateRecord:
            # The run/item association is idempotent; a retry or resume keeps it.
            pass
        existing = await self._runs.list_steps(run_id)
        by_capability: dict[tuple[str, str], StepRecord] = {}
        for step in existing:
            key = (step.capability_id, step.capability_version)
            if key in by_capability:
                raise DuplicateNodeError(f"{step.capability_id}@{step.capability_version}")
            by_capability[key] = step
            self._steps[str(step.id)] = step

        records: list[StepRecord] = []
        seen: set[tuple[str, str]] = set()
        for node in plan.ordered_nodes:
            key = (node.capability_id, node.capability_version)
            if key in seen:
                raise DuplicateNodeError(node.node_id)
            seen.add(key)
            record = by_capability.get(key)
            if record is None:
                record = await self._runs.create_step(
                    run_id,
                    item_id,
                    node.capability_id,
                    node.capability_version,
                    status=StepStatus.PENDING,
                )
                self._steps[str(record.id)] = record
            records.append(record)
        await self._commit()
        return tuple(records)

    async def set_item_status(
        self,
        run_id: RunId,
        item_id: ItemId,
        status: RunItemStatus | str,
    ) -> None:
        await self._runs.set_item_status(run_id, item_id, status)
        await self._commit()

    async def list_steps(self, run_id: RunId) -> tuple[StepRecord, ...]:
        steps = await self._runs.list_steps(run_id)
        for step in steps:
            self._steps[str(step.id)] = step
        return tuple(steps)

    def step(self, step_id: StepId) -> StepRecord:
        record = self._steps.get(str(step_id))
        if record is None:
            raise StepNotFound(str(step_id))
        return record

    def _remember(self, record: StepRecord) -> StepRecord:
        self._steps[str(record.id)] = record
        return record

    async def step_ready(self, step_id: StepId) -> StepRecord:
        current = self.step(step_id)
        StepStateMachine.validate(current.status, StepStatus.READY)
        updated = await self._runs.update_step(step_id, status=StepStatus.READY)
        await self._commit()
        return self._remember(updated)

    async def step_started(
        self,
        step_id: StepId,
        *,
        attempts: int | None = None,
    ) -> StepRecord:
        current = self.step(step_id)
        StepStateMachine.validate(current.status, StepStatus.RUNNING)
        next_attempt = current.attempts + 1 if attempts is None else attempts
        updated = await self._runs.update_step(
            step_id,
            status=StepStatus.RUNNING,
            attempts=next_attempt,
            started_at=self._clock(),
        )
        await self._commit()
        return self._remember(updated)

    async def set_step_cache_key(self, step_id: StepId, cache_key: str) -> StepRecord:
        updated = await self._runs.update_step(step_id, cache_key=cache_key)
        await self._commit()
        return self._remember(updated)

    async def reset_step(self, step_id: StepId, *, reason: str | None = None) -> StepRecord:
        """Return a step to pending when its cached artifact cannot be restored."""

        current = self.step(step_id)
        StepStateMachine.validate(current.status, StepStatus.PENDING, recovering=True)
        updated = await self._runs.update_step(step_id, status=StepStatus.PENDING)
        if reason is not None:
            updated = await self._runs.update_step(
                step_id,
                warnings=(*updated.warnings, reason),
            )
        await self._commit()
        return self._remember(updated)

    async def step_cached(
        self,
        step_id: StepId,
        *,
        cache_key: str | None = None,
        warnings: Iterable[StepWarning | Mapping[str, Any] | str] | None = None,
    ) -> StepRecord:
        return await self._transition_step(
            step_id,
            StepStatus.CACHED,
            cache_key=cache_key,
            warnings=warnings,
            recovering=True,
        )

    async def step_succeeded(
        self,
        step_id: StepId,
        *,
        cache_key: str | None = None,
        warnings: Iterable[StepWarning | Mapping[str, Any] | str] | None = None,
    ) -> StepRecord:
        return await self._transition_step(
            step_id,
            StepStatus.SUCCEEDED,
            cache_key=cache_key,
            warnings=warnings,
            recovering=True,
        )

    async def step_skipped(
        self,
        step_id: StepId,
        *,
        warnings: Iterable[StepWarning | Mapping[str, Any] | str] | None = None,
        cache_key: str | None = None,
    ) -> StepRecord:
        return await self._transition_step(
            step_id,
            StepStatus.SKIPPED,
            cache_key=cache_key,
            warnings=warnings,
            recovering=True,
        )

    async def step_failed(
        self,
        step_id: StepId,
        *,
        error: BaseException | Mapping[str, Any] | None = None,
        warnings: Iterable[StepWarning | Mapping[str, Any] | str] | None = None,
        cache_key: str | None = None,
    ) -> StepRecord:
        return await self._transition_step(
            step_id,
            StepStatus.FAILED,
            cache_key=cache_key,
            warnings=warnings,
            error=error,
            recovering=True,
        )

    async def _transition_step(
        self,
        step_id: StepId,
        target: StepStatus,
        *,
        cache_key: str | None,
        warnings: Iterable[StepWarning | Mapping[str, Any] | str] | None,
        error: BaseException | Mapping[str, Any] | None = None,
        recovering: bool = False,
    ) -> StepRecord:
        current = self.step(step_id)
        StepStateMachine.validate(current.status, target, recovering=recovering)
        updated = await self._runs.update_step(
            step_id,
            status=target,
            finished_at=self._clock(),
            warnings=_warning_payload(warnings),
            cache_key=cache_key,
            error=_error_payload(error),
        )
        await self._commit()
        return self._remember(updated)

    # -- resume -------------------------------------------------------

    async def resume(self, run_id: RunId) -> RecoveryReport:
        """Validate a run and recover its steps after a crash.

        Completed runs raise :class:`RunAlreadyCompleted`; other terminal
        states raise :class:`RunNotResumable`.  Valid cached artifacts move a
        running/pending step to ``cached``; missing artifacts move a running
        or ready step back to ``pending``.
        """

        run = await self.get_run(run_id)
        if run.status is RunStatus.COMPLETED:
            raise RunAlreadyCompleted(str(run_id))
        if run.status not in (RunStatus.RUNNING, RunStatus.PAUSED):
            raise RunNotResumable(str(run_id), run.status.value)

        steps = await self.list_steps(run_id)
        cached: list[StepId] = []
        pending: list[StepId] = []
        for step in steps:
            if step.status is StepStatus.RUNNING:
                if await self._has_valid_artifact(step):
                    updated = await self._runs.update_step(
                        step.id,
                        status=StepStatus.CACHED,
                        finished_at=self._clock(),
                    )
                    self._remember(updated)
                    cached.append(step.id)
                else:
                    updated = await self._runs.update_step(step.id, status=StepStatus.PENDING)
                    self._remember(updated)
                    pending.append(step.id)
            elif step.status is StepStatus.READY:
                if await self._has_valid_artifact(step):
                    updated = await self._runs.update_step(
                        step.id,
                        status=StepStatus.CACHED,
                        finished_at=self._clock(),
                    )
                    self._remember(updated)
                    cached.append(step.id)
                else:
                    updated = await self._runs.update_step(step.id, status=StepStatus.PENDING)
                    self._remember(updated)
                    pending.append(step.id)
            elif step.status is StepStatus.PENDING:
                if await self._has_valid_artifact(step):
                    updated = await self._runs.update_step(
                        step.id,
                        status=StepStatus.CACHED,
                        finished_at=self._clock(),
                    )
                    self._remember(updated)
                    cached.append(step.id)
                else:
                    pending.append(step.id)
            elif step.status is StepStatus.CACHED:
                cached.append(step.id)
        if run.status is RunStatus.PAUSED:
            run = await self._runs.update_run(run_id, status=RunStatus.RUNNING)
            self._runs_by_id[str(run_id)] = run
        await self._commit()
        refreshed = tuple(self._steps[str(step.id)] for step in steps)
        return RecoveryReport(
            run=run,
            steps=refreshed,
            cached=tuple(cached),
            pending=tuple(pending),
        )

    async def _has_valid_artifact(self, step: StepRecord) -> bool:
        if self._cache is None or step.cache_key is None:
            return False
        return await self._cache.has(step.cache_key)

    def step_by_capability(
        self,
        capability_id: str,
        capability_version: str,
    ) -> StepRecord | None:
        for record in self._steps.values():
            if (
                record.capability_id == capability_id
                and record.capability_version == capability_version
            ):
                return record
        return None

    def reset_cache(self) -> None:
        """Forget cached step records (useful between runs in one process)."""

        self._steps.clear()
        self._runs_by_id.clear()


__all__ = [
    "JobManager",
    "RecoveryReport",
    "RunStateMachine",
    "StepStateMachine",
]
