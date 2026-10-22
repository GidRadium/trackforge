"""Executor tests: DAG execution, cache, retry, fallback, cancellation."""

from __future__ import annotations

import asyncio

import pytest

from trackforge.adapters.persistence.sqlite.unit_of_work import SqliteUnitOfWork
from trackforge.application.dto.db import RunItemStatus, RunStatus, StepStatus
from trackforge.application.ports.execution import (
    ArtifactDraft,
    Capability,
    StepResult,
    StepResultStatus,
    StepWarning,
)
from trackforge.domain.errors import ProviderUnavailable
from trackforge.domain.model.capabilities import ArtifactType, CapabilityKind, Cost
from trackforge.domain.model.ids import ItemId, RunId, new_uuid7
from trackforge.domain.model.item import Item, ItemStatus
from trackforge.pipeline.budget import Budget
from trackforge.pipeline.errors import PlanBlocked, UnknownCapability
from trackforge.pipeline.events import EventType
from trackforge.pipeline.executor import Executor

pytestmark = pytest.mark.unit

MEDIA = ArtifactType.parse("media.raw.v1")
PROBE = ArtifactType.parse("audio.probe.v1")
FEATURES = ArtifactType.parse("audio.features.v1")
ITEM = ItemId(new_uuid7())


def three_step_caps(make_manifest, make_capability):
    acquire = make_capability(
        make_manifest(
            "acquire",
            provides=(MEDIA,),
            kind=CapabilityKind.PROVIDER,
        )
    )
    probe = make_capability(make_manifest("probe", requires=(MEDIA,), provides=(PROBE,)))
    features = make_capability(make_manifest("features", requires=(PROBE,), provides=(FEATURES,)))
    return [acquire, probe, features]


def test_fake_capability_satisfies_protocol_and_context(make_capability, make_manifest) -> None:
    capability = make_capability(make_manifest("probe", provides=(PROBE,)))
    assert isinstance(capability, Capability)


@pytest.mark.asyncio
async def test_three_step_dag_executes_and_emits_events(
    make_manifest,
    make_capability,
    make_plan,
    make_executor,
    fake_runs,
    event_bus,
) -> None:
    caps = three_step_caps(make_manifest, make_capability)
    plan = make_plan(caps, [FEATURES], required=[FEATURES])
    executor = make_executor(caps, runs=fake_runs, event_bus_override=event_bus)

    result = await executor.run(plan, item_id=ITEM)

    assert result.status is RunStatus.COMPLETED
    assert [(step.capability_id, step.status) for step in result.steps] == [
        ("acquire", StepStatus.SUCCEEDED),
        ("probe", StepStatus.SUCCEEDED),
        ("features", StepStatus.SUCCEEDED),
    ]
    assert [capability.call_count for capability in caps] == [1, 1, 1]
    assert result.artifact(FEATURES) is not None
    assert result.warnings == ()
    assert fake_runs.items[(str(result.run_id), str(ITEM))].status is RunItemStatus.SUCCEEDED

    event_types = [event.type for event in event_bus.history(result.run_id)]
    assert event_types[0] is EventType.RUN_STARTED
    assert event_types[-1] is EventType.RUN_FINISHED
    assert event_types.count(EventType.STEP_STARTED) == 3
    assert event_types.count(EventType.STEP_FINISHED) == 3


@pytest.mark.asyncio
async def test_second_run_is_served_from_cache(
    make_manifest,
    make_capability,
    make_plan,
    make_executor,
    fake_runs,
) -> None:
    caps = three_step_caps(make_manifest, make_capability)
    plan = make_plan(caps, [FEATURES], required=[FEATURES])
    executor = make_executor(caps, runs=fake_runs)

    first = await executor.run(plan, item_id=ITEM)
    calls_after_first = [capability.call_count for capability in caps]
    second = await executor.run(plan, item_id=ITEM)

    assert first.status is RunStatus.COMPLETED
    assert second.status is RunStatus.COMPLETED
    assert [step.status for step in second.steps] == [
        StepStatus.CACHED,
        StepStatus.CACHED,
        StepStatus.CACHED,
    ]
    assert [capability.call_count for capability in caps] == calls_after_first
    assert [step.attempts for step in second.steps] == [0, 0, 0]


@pytest.mark.asyncio
async def test_force_step_bypasses_only_selected_cache(
    make_manifest,
    make_capability,
    make_plan,
    make_executor,
    fake_runs,
) -> None:
    caps = three_step_caps(make_manifest, make_capability)
    plan = make_plan(caps, [FEATURES], required=[FEATURES])
    executor = make_executor(caps, runs=fake_runs)

    await executor.run(plan, item_id=ITEM)
    acquire, probe, features = caps
    await executor.run(plan, item_id=ITEM, force_steps={"probe"})

    assert acquire.call_count == 1
    assert probe.call_count == 2
    assert features.call_count == 2


@pytest.mark.asyncio
async def test_force_all_bypasses_cache(
    make_manifest,
    make_capability,
    make_plan,
    make_executor,
    fake_runs,
) -> None:
    caps = three_step_caps(make_manifest, make_capability)
    plan = make_plan(caps, [FEATURES], required=[FEATURES])
    executor = make_executor(caps, runs=fake_runs)

    await executor.run(plan, item_id=ITEM)
    await executor.run(plan, item_id=ITEM, force_all=True)

    assert [capability.call_count for capability in caps] == [2, 2, 2]


@pytest.mark.asyncio
async def test_retry_then_fallback_to_second_provider(
    make_manifest,
    make_capability,
    make_plan,
    make_executor,
    fake_runs,
) -> None:
    acquire = make_capability(
        make_manifest("acquire", provides=(MEDIA,), kind=CapabilityKind.PROVIDER)
    )
    failing = make_capability(
        make_manifest("first", requires=(MEDIA,), provides=(PROBE,)),
        behaviors=[ProviderUnavailable("down")] * 3,
    )
    succeeding = make_capability(make_manifest("second", requires=(MEDIA,), provides=(PROBE,)))
    plan = make_plan([acquire, failing, succeeding], [PROBE], required=[PROBE])
    executor = make_executor([acquire, failing, succeeding], runs=fake_runs)

    result = await executor.run(plan, item_id=ITEM)

    assert result.status is RunStatus.COMPLETED
    assert failing.call_count == 3
    assert succeeding.call_count == 1
    by_capability = {step.capability_id: step for step in result.steps}
    assert by_capability["first"].status is StepStatus.FAILED
    assert by_capability["first"].attempts == 3
    assert by_capability["second"].status is StepStatus.SUCCEEDED
    assert "provider_unavailable" in {warning.code for warning in result.warnings}
    assert "retry_exhausted" in {warning.code for warning in result.warnings}


@pytest.mark.asyncio
async def test_non_retryable_failure_is_not_retried(
    make_manifest,
    make_capability,
    make_plan,
    make_executor,
    fake_runs,
) -> None:
    failing = make_capability(
        make_manifest("probe", provides=(PROBE,)),
        behaviors=[ValueError("broken")],
    )
    plan = make_plan([failing], [PROBE], required=[PROBE])
    executor = make_executor([failing], runs=fake_runs, max_attempts=3)

    result = await executor.run(plan, item_id=ITEM)

    assert result.status is RunStatus.FAILED
    assert failing.call_count == 1
    assert result.steps[0].status is StepStatus.FAILED
    assert result.steps[0].attempts == 1


@pytest.mark.asyncio
async def test_optional_failure_completes_with_warning(
    make_manifest,
    make_capability,
    make_plan,
    make_executor,
    fake_runs,
) -> None:
    failing = make_capability(
        make_manifest("probe", provides=(PROBE,)),
        behaviors=[ValueError("broken")],
    )
    plan = make_plan([failing], [PROBE])
    executor = make_executor([failing], runs=fake_runs, max_attempts=1)

    result = await executor.run(plan, item_id=ITEM)

    assert result.status is RunStatus.COMPLETED
    assert result.steps[0].status is StepStatus.FAILED
    assert {"capability_failed", "no_data"} <= {warning.code for warning in result.warnings}


@pytest.mark.asyncio
async def test_required_failure_fails_run(
    make_manifest,
    make_capability,
    make_plan,
    make_executor,
    fake_runs,
) -> None:
    failing = make_capability(
        make_manifest("probe", provides=(PROBE,)),
        behaviors=[ValueError("broken")],
    )
    plan = make_plan([failing], [PROBE], required=[PROBE])
    executor = make_executor([failing], runs=fake_runs, max_attempts=1)

    result = await executor.run(plan, item_id=ITEM)

    assert result.status is RunStatus.FAILED
    assert result.error is not None
    assert result.error["required_outputs"] == [PROBE.render()]


@pytest.mark.asyncio
async def test_degraded_result_is_visible_and_completes(
    make_manifest,
    make_capability,
    make_plan,
    make_executor,
    fake_runs,
    event_bus,
) -> None:
    degraded = StepResult(
        artifacts=(ArtifactDraft(type=PROBE, payload={"partial": True}),),
        warnings=(StepWarning(code="no_data", message="no tags"),),
        status=StepResultStatus.DEGRADED,
    )
    capability = make_capability(
        make_manifest("probe", provides=(PROBE,)),
        behaviors=[degraded],
    )
    plan = make_plan([capability], [PROBE], required=[PROBE])
    executor = make_executor([capability], runs=fake_runs, event_bus_override=event_bus)

    result = await executor.run(plan, item_id=ITEM)

    assert result.status is RunStatus.COMPLETED
    assert result.steps[0].status is StepStatus.SUCCEEDED
    assert "no_data" in result.steps[0].warning_codes
    finished = [
        event for event in event_bus.history(result.run_id) if event.type is EventType.STEP_FINISHED
    ]
    assert finished[0].status == "degraded"
    assert finished[0].warning_codes == ("no_data",)


@pytest.mark.asyncio
async def test_skipped_result_allows_next_provider(
    make_manifest,
    make_capability,
    make_plan,
    make_executor,
    fake_runs,
) -> None:
    skipped = make_capability(
        make_manifest("first", provides=(PROBE,)),
        behaviors=[StepResult(status=StepResultStatus.SKIPPED)],
    )
    succeeding = make_capability(make_manifest("second", provides=(PROBE,)))
    plan = make_plan([skipped, succeeding], [PROBE], required=[PROBE])
    executor = make_executor([skipped, succeeding], runs=fake_runs, max_attempts=1)

    result = await executor.run(plan, item_id=ITEM)

    assert result.status is RunStatus.COMPLETED
    assert skipped.call_count == 1
    assert succeeding.call_count == 1
    by_capability = {step.capability_id: step for step in result.steps}
    assert by_capability["first"].status is StepStatus.SKIPPED
    assert by_capability["second"].status is StepStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_invalid_artifact_type_is_rejected(
    make_manifest,
    make_capability,
    make_plan,
    make_executor,
    fake_runs,
) -> None:
    invalid = StepResult(artifacts=(ArtifactDraft(type=FEATURES, payload={}),))
    capability = make_capability(
        make_manifest("probe", provides=(PROBE,)),
        behaviors=[invalid, invalid],
    )
    plan = make_plan([capability], [PROBE], required=[PROBE])
    executor = make_executor([capability], runs=fake_runs, max_attempts=2)

    result = await executor.run(plan, item_id=ITEM)

    assert result.status is RunStatus.FAILED
    assert capability.call_count == 1
    assert result.artifact(FEATURES) is None
    assert "invalid_output" in {warning.code for warning in result.warnings}
    assert isinstance(
        capability.calls[0].inputs,
        dict,
    )


@pytest.mark.asyncio
async def test_cancellation_during_step_marks_run_cancelled(
    make_manifest,
    make_capability,
    make_plan,
    make_executor,
    fake_runs,
    event_bus,
) -> None:
    capability = make_capability(
        make_manifest("slow", provides=(PROBE,)),
        delay=1.0,
    )
    plan = make_plan([capability], [PROBE], required=[PROBE])
    executor = make_executor([capability], runs=fake_runs, event_bus_override=event_bus)
    task = asyncio.create_task(executor.run(plan, item_id=ITEM))

    run_id: str | None = None
    for _ in range(200):
        if event_bus._history:
            run_id = next(iter(event_bus._history))
            if any(event.type is EventType.STEP_STARTED for event in event_bus._history[run_id]):
                break
        await asyncio.sleep(0.01)
    assert run_id is not None
    executor.cancel(RunId(run_id))
    result = await asyncio.wait_for(task, timeout=3)

    assert result.status is RunStatus.CANCELLED
    assert all(step.status is not StepStatus.RUNNING for step in result.steps)
    assert all(step.status is not StepStatus.RUNNING for step in fake_runs.steps.values())
    assert EventType.RUN_CANCELLED in [event.type for event in event_bus.history(result.run_id)]


@pytest.mark.asyncio
async def test_blocked_plan_is_rejected(
    make_manifest, make_capability, make_plan, make_executor
) -> None:
    plan = make_plan([], [PROBE], required=[PROBE])
    executor = make_executor([])
    with pytest.raises(PlanBlocked):
        await executor.run(plan, item_id=ITEM)


@pytest.mark.asyncio
async def test_unknown_capability_is_rejected(
    make_manifest,
    make_capability,
    make_plan,
    make_executor,
    fake_runs,
) -> None:
    capability = make_capability(make_manifest("probe", provides=(PROBE,)))
    plan = make_plan([capability], [PROBE], required=[PROBE])
    executor = make_executor([])
    with pytest.raises(UnknownCapability):
        await executor.run(plan, item_id=ITEM)


@pytest.mark.asyncio
async def test_runtime_budget_skips_required_step(
    make_manifest,
    make_capability,
    make_plan,
    make_executor,
    fake_runs,
) -> None:
    capability = make_capability(
        make_manifest("probe", provides=(PROBE,), cost=Cost(cpu_seconds=10))
    )
    plan = make_plan([capability], [PROBE], required=[PROBE])
    executor = make_executor(
        [capability],
        runs=fake_runs,
        budget=Budget(max_cpu_seconds=1),
    )

    result = await executor.run(plan, item_id=ITEM)

    assert result.status is RunStatus.FAILED
    assert capability.call_count == 0
    assert result.steps[0].status is StepStatus.SKIPPED
    assert "budget_exceeded" in {warning.code for warning in result.warnings}


@pytest.mark.asyncio
async def test_executor_step_context_carries_inputs_and_budget(
    make_manifest,
    make_capability,
    make_plan,
    make_executor,
    fake_runs,
) -> None:
    acquire = make_capability(
        make_manifest("acquire", provides=(MEDIA,), kind=CapabilityKind.PROVIDER)
    )
    probe = make_capability(
        make_manifest("probe", requires=(MEDIA,), provides=(PROBE,)),
        behaviors=[
            lambda ctx: StepResult(artifacts=(ArtifactDraft(type=PROBE, payload={"seen": True}),))
        ],
    )
    plan = make_plan([acquire, probe], [PROBE], required=[PROBE])
    executor = make_executor([acquire, probe], runs=fake_runs)

    await executor.run(plan, item_id=ITEM)

    context = probe.calls[0]
    assert context.run_id
    assert context.item_id == ITEM
    assert MEDIA in context.inputs
    assert context.inputs[MEDIA].payload
    assert context.budget.cpu_seconds is None
    assert context.force is False
    assert context.cancelled.is_set() is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_filesystem_store_and_sqlite_catalog(
    catalog,
    artifact_store,
    event_bus,
    make_manifest,
    make_capability,
    make_plan,
) -> None:
    async with SqliteUnitOfWork(catalog) as unit:
        item = Item(id=ItemId(new_uuid7()), status=ItemStatus.DISCOVERED)
        await unit.items.save(item)
        await unit.commit()
        caps = three_step_caps(make_manifest, make_capability)
        plan = make_plan(caps, [FEATURES], required=[FEATURES])
        executor = Executor(
            caps,
            artifact_store,
            unit.runs,
            events=event_bus,
            commit=unit.commit,
        )

        first = await executor.run(plan, item_id=item.id)
        persisted = await unit.runs.get_run(first.run_id)
        calls_after_first = [capability.call_count for capability in caps]
        second = await executor.run(plan, item_id=item.id)

        assert first.status is RunStatus.COMPLETED
        assert persisted is not None and persisted.status is RunStatus.COMPLETED
        assert second.status is RunStatus.COMPLETED
        assert [step.status for step in second.steps] == [
            StepStatus.CACHED,
            StepStatus.CACHED,
            StepStatus.CACHED,
        ]
        assert [capability.call_count for capability in caps] == calls_after_first
        assert first.run_id != second.run_id


@pytest.mark.asyncio
async def test_degraded_provider_still_allows_a_better_fallback(
    make_manifest,
    make_capability,
    make_plan,
    make_executor,
    fake_runs,
) -> None:
    degraded = StepResult(
        artifacts=(ArtifactDraft(type=PROBE, payload={"partial": True}),),
        warnings=(StepWarning(code="no_data", message="partial"),),
        status=StepResultStatus.DEGRADED,
    )
    first = make_capability(
        make_manifest("first", provides=(PROBE,)),
        behaviors=[degraded],
    )
    second = make_capability(make_manifest("second", provides=(PROBE,)))
    plan = make_plan([first, second], [PROBE], required=[PROBE])
    executor = make_executor([first, second], runs=fake_runs)

    result = await executor.run(plan, item_id=ITEM)

    assert result.status is RunStatus.COMPLETED
    assert first.call_count == 1
    assert second.call_count == 1
    assert result.artifact(PROBE) is not None
    assert result.artifact(PROBE).payload_hash


def test_fake_store_and_run_repository_satisfy_ports(
    fake_store,
    fake_runs,
) -> None:
    from trackforge.application.ports.repositories import RunRepository
    from trackforge.application.ports.storage import ArtifactStore

    assert isinstance(fake_store, ArtifactStore)
    assert isinstance(fake_runs, RunRepository)
