"""State machine and resume tests."""

from __future__ import annotations

import pytest

from trackforge.adapters.persistence.sqlite.unit_of_work import SqliteUnitOfWork
from trackforge.application.dto.db import RunStatus, StepStatus
from trackforge.application.ports.execution import ArtifactDraft, StepResult
from trackforge.domain.model.capabilities import ArtifactType, CapabilityKind
from trackforge.domain.model.ids import ItemId, RunId, new_uuid7
from trackforge.domain.model.item import Item, ItemStatus
from trackforge.pipeline.cache import Cache, cache_key_for_step
from trackforge.pipeline.errors import (
    InvalidTransition,
    RunAlreadyCompleted,
    RunNotFound,
    RunNotResumable,
)
from trackforge.pipeline.executor import Executor
from trackforge.pipeline.jobs import JobManager, RunStateMachine, StepStateMachine

pytestmark = pytest.mark.unit

MEDIA = ArtifactType.parse("media.raw.v1")
PROBE = ArtifactType.parse("audio.probe.v1")
ITEM = ItemId(new_uuid7())


def test_run_state_machine_accepts_and_rejects_transitions() -> None:
    assert RunStateMachine.can_transition(RunStatus.PLANNED, RunStatus.RUNNING)
    assert RunStateMachine.can_transition(RunStatus.RUNNING, RunStatus.PAUSED)
    assert RunStateMachine.can_transition(RunStatus.PAUSED, RunStatus.RUNNING)
    assert RunStateMachine.can_transition(RunStatus.RUNNING, RunStatus.COMPLETED)
    assert RunStateMachine.can_transition(RunStatus.RUNNING, RunStatus.FAILED)
    assert RunStateMachine.can_transition(RunStatus.RUNNING, RunStatus.CANCELLED)
    assert not RunStateMachine.can_transition(RunStatus.COMPLETED, RunStatus.RUNNING)
    assert not RunStateMachine.can_transition(RunStatus.PLANNED, RunStatus.COMPLETED)
    with pytest.raises(InvalidTransition):
        RunStateMachine.validate(RunStatus.COMPLETED, RunStatus.RUNNING)
    assert RunStateMachine.terminal(RunStatus.CANCELLED) is True


def test_step_state_machine_accepts_and_rejects_transitions() -> None:
    assert StepStateMachine.can_transition(StepStatus.PENDING, StepStatus.READY)
    assert StepStateMachine.can_transition(StepStatus.READY, StepStatus.RUNNING)
    assert StepStateMachine.can_transition(StepStatus.RUNNING, StepStatus.SUCCEEDED)
    assert StepStateMachine.can_transition(StepStatus.RUNNING, StepStatus.FAILED)
    assert not StepStateMachine.can_transition(StepStatus.PENDING, StepStatus.RUNNING)
    assert not StepStateMachine.can_transition(StepStatus.SUCCEEDED, StepStatus.RUNNING)
    assert StepStateMachine.can_transition(
        StepStatus.RUNNING,
        StepStatus.PENDING,
        recovering=True,
    )
    with pytest.raises(InvalidTransition):
        StepStateMachine.validate(StepStatus.SUCCEEDED, StepStatus.RUNNING)
    assert StepStateMachine.terminal(StepStatus.CACHED) is True


@pytest.mark.asyncio
async def test_job_manager_lifecycle_and_idempotent_complete(fake_runs) -> None:
    jobs = JobManager(fake_runs)
    run = await jobs.create_run("test", "auto")
    assert run.status is RunStatus.PLANNED
    started = await jobs.start(run.id)
    assert started.status is RunStatus.RUNNING
    paused = await jobs.pause(run.id)
    assert paused.status is RunStatus.PAUSED
    resumed = await jobs.start(run.id)
    assert resumed.status is RunStatus.RUNNING
    completed = await jobs.complete(run.id)
    assert completed.status is RunStatus.COMPLETED
    again = await jobs.complete(run.id)
    assert again.status is RunStatus.COMPLETED
    with pytest.raises(RunAlreadyCompleted):
        await jobs.start(run.id)


@pytest.mark.asyncio
async def test_resume_unknown_run_raises(fake_runs) -> None:
    jobs = JobManager(fake_runs)
    with pytest.raises(RunNotFound):
        await jobs.resume(RunId(new_uuid7()))


@pytest.mark.asyncio
async def test_resume_terminal_run_is_rejected(fake_runs) -> None:
    jobs = JobManager(fake_runs)
    run = await jobs.create_run("test", "auto")
    await jobs.start(run.id)
    await jobs.complete(run.id)
    with pytest.raises(RunAlreadyCompleted):
        await jobs.resume(run.id)

    cancelled = await jobs.create_run("test", "auto")
    await jobs.start(cancelled.id)
    await jobs.cancel(cancelled.id)
    with pytest.raises(RunNotResumable):
        await jobs.resume(cancelled.id)


@pytest.mark.asyncio
async def test_running_step_without_artifact_returns_to_pending(
    fake_runs,
    make_manifest,
    make_plan,
    make_capability,
) -> None:
    manifest = make_manifest("probe", provides=(PROBE,))
    capability = make_capability(manifest)
    plan = make_plan([capability], [PROBE], required=[PROBE])
    jobs = JobManager(fake_runs)
    run = await jobs.create_run("test", "auto")
    await jobs.start(run.id)
    steps = await jobs.ensure_steps(run.id, ITEM, plan)
    await fake_runs.update_step(steps[0].id, status=StepStatus.RUNNING)

    report = await jobs.resume(run.id)

    assert report.pending == (steps[0].id,)
    record = fake_runs.steps[str(steps[0].id)]
    assert record.status is StepStatus.PENDING


@pytest.mark.asyncio
async def test_pending_step_with_valid_cache_becomes_cached(
    fake_runs,
    fake_store,
    make_manifest,
    make_plan,
    make_capability,
) -> None:
    manifest = make_manifest("probe", provides=(PROBE,))
    capability = make_capability(manifest)
    plan = make_plan([capability], [PROBE], required=[PROBE])
    jobs = JobManager(fake_runs, cache=Cache(fake_store))
    run = await jobs.create_run("test", "auto")
    await jobs.start(run.id)
    steps = await jobs.ensure_steps(run.id, ITEM, plan)
    key = cache_key_for_step({}, manifest, {})
    await Cache(fake_store).store(
        key,
        StepResult(artifacts=(ArtifactDraft(type=PROBE, payload={"cached": True}),)),
        producer=manifest,
    )
    await fake_runs.update_step(steps[0].id, cache_key=key)

    report = await jobs.resume(run.id)

    assert report.cached == (steps[0].id,)
    assert fake_runs.steps[str(steps[0].id)].status is StepStatus.CACHED


@pytest.mark.integration
@pytest.mark.asyncio
async def test_crash_between_steps_resume_does_not_repeat_success(
    catalog,
    artifact_store,
    make_manifest,
    make_capability,
    make_plan,
) -> None:
    caps = [
        make_capability(make_manifest("acquire", provides=(MEDIA,), kind=CapabilityKind.PROVIDER)),
        make_capability(make_manifest("probe", requires=(MEDIA,), provides=(PROBE,))),
    ]
    plan = make_plan(caps, [PROBE], required=[PROBE])
    item = Item(id=ItemId(new_uuid7()), status=ItemStatus.DISCOVERED)

    async with SqliteUnitOfWork(catalog) as unit:
        await unit.items.save(item)
        await unit.commit()
        cache = Cache(artifact_store)
        jobs = JobManager(unit.runs, cache=cache, commit=unit.commit)
        run = await jobs.create_run("test", "auto")
        await jobs.start(run.id)
        steps = await jobs.ensure_steps(run.id, item.id, plan)
        first, second = steps
        key = cache_key_for_step({}, caps[0].manifest, {})
        await cache.store(
            key,
            StepResult(artifacts=(ArtifactDraft(type=MEDIA, payload={"calls": 1}),)),
            producer=caps[0].manifest,
        )
        await unit.runs.update_step(
            first.id,
            status=StepStatus.SUCCEEDED,
            cache_key=key,
        )
        await unit.runs.update_step(second.id, status=StepStatus.RUNNING)
        await unit.commit()

    resumed_caps = [
        make_capability(make_manifest("acquire", provides=(MEDIA,), kind=CapabilityKind.PROVIDER)),
        make_capability(make_manifest("probe", requires=(MEDIA,), provides=(PROBE,))),
    ]
    async with SqliteUnitOfWork(catalog) as unit:
        executor = Executor(
            resumed_caps,
            artifact_store,
            unit.runs,
            commit=unit.commit,
        )
        result = await executor.resume(run.id, plan, item_id=item.id)

        assert result.status is RunStatus.COMPLETED
        assert resumed_caps[0].call_count == 0
        assert resumed_caps[1].call_count == 1
        steps_by_capability = {step.capability_id: step for step in result.steps}
        assert steps_by_capability["acquire"].status is StepStatus.SUCCEEDED
        assert steps_by_capability["probe"].status is StepStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_resume_recomputes_a_succeeded_step_with_missing_artifact(
    fake_runs,
    fake_store,
    make_manifest,
    make_capability,
    make_plan,
) -> None:
    manifest = make_manifest("probe", provides=(PROBE,))
    capability = make_capability(manifest)
    plan = make_plan([capability], [PROBE], required=[PROBE])
    jobs = JobManager(fake_runs, cache=Cache(fake_store))
    run = await jobs.create_run("test", "auto")
    await jobs.start(run.id)
    steps = await jobs.ensure_steps(run.id, ITEM, plan)
    await fake_runs.update_step(
        steps[0].id,
        status=StepStatus.SUCCEEDED,
        cache_key="a" * 64,
    )
    executor = Executor(
        [capability],
        fake_store,
        fake_runs,
        cache=Cache(fake_store),
    )

    result = await executor.resume(run.id, plan, item_id=ITEM)

    assert result.status is RunStatus.COMPLETED
    assert capability.call_count == 1
    assert result.steps[0].status is StepStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_resume_paused_run_transitions_to_running(
    fake_runs,
    make_manifest,
    make_capability,
    make_plan,
) -> None:
    capability = make_capability(make_manifest("probe", provides=(PROBE,)))
    plan = make_plan([capability], [PROBE], required=[PROBE])
    jobs = JobManager(fake_runs)
    run = await jobs.create_run("test", "auto")
    await jobs.start(run.id)
    await jobs.ensure_steps(run.id, ITEM, plan)
    await jobs.pause(run.id)

    report = await jobs.resume(run.id)

    assert report.run.status is RunStatus.RUNNING
    assert fake_runs.runs[str(run.id)].status is RunStatus.RUNNING
