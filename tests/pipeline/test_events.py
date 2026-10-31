"""Unit tests for the in-process event bus."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from trackforge.domain.model.ids import RunId, new_uuid7
from trackforge.pipeline.events import Event, EventBus, EventType

pytestmark = pytest.mark.unit


def make_event(run_id: RunId, event_type: EventType = EventType.RUN_STARTED) -> Event:
    return Event(type=event_type, run_id=run_id, status="running")


async def wait_for_subscriber(bus: EventBus, run_id: RunId) -> None:
    for _ in range(100):
        if bus._subscribers.get(str(run_id)):
            return
        await asyncio.sleep(0)
    raise AssertionError("subscriber did not register")


def test_publish_assigns_sequence_and_keeps_history() -> None:
    bus = EventBus()
    run_id = RunId(new_uuid7())
    first = bus.publish(make_event(run_id))
    second = bus.publish(Event(type=EventType.STEP_STARTED, run_id=run_id, status="running"))
    assert first.sequence == 1
    assert second.sequence == 2
    assert bus.history(run_id) == (first, second)
    assert bus.is_closed(run_id) is False


def test_terminal_event_closes_run() -> None:
    bus = EventBus()
    run_id = RunId(new_uuid7())
    bus.publish(Event(type=EventType.RUN_FINISHED, run_id=run_id, status="completed"))
    assert bus.is_closed(run_id) is True


@pytest.mark.asyncio
async def test_subscriber_receives_events_until_terminal() -> None:
    bus = EventBus()
    run_id = RunId(new_uuid7())

    async def collect() -> list[Event]:
        return [event async for event in bus.events(run_id)]

    task = asyncio.create_task(collect())
    await wait_for_subscriber(bus, run_id)
    bus.publish(make_event(run_id))
    bus.publish(Event(type=EventType.STEP_STARTED, run_id=run_id, status="running"))
    bus.publish(Event(type=EventType.RUN_FINISHED, run_id=run_id, status="completed"))
    events = await asyncio.wait_for(task, timeout=1)

    assert [event.type for event in events] == [
        EventType.RUN_STARTED,
        EventType.STEP_STARTED,
        EventType.RUN_FINISHED,
    ]
    assert [event.sequence for event in events] == [1, 2, 3]


@pytest.mark.asyncio
async def test_late_subscriber_replays_history_and_stops_at_terminal() -> None:
    bus = EventBus()
    run_id = RunId(new_uuid7())
    bus.publish(make_event(run_id))
    bus.publish(Event(type=EventType.RUN_FINISHED, run_id=run_id, status="completed"))

    events = [event async for event in bus.events(run_id)]
    assert [event.type for event in events] == [
        EventType.RUN_STARTED,
        EventType.RUN_FINISHED,
    ]


@pytest.mark.asyncio
async def test_replay_false_skips_old_events() -> None:
    bus = EventBus()
    run_id = RunId(new_uuid7())
    bus.publish(make_event(run_id))

    async def collect() -> list[Event]:
        return [event async for event in bus.events(run_id, replay=False)]

    task = asyncio.create_task(collect())
    await wait_for_subscriber(bus, run_id)
    bus.publish(Event(type=EventType.RUN_FINISHED, run_id=run_id, status="completed"))
    events = await asyncio.wait_for(task, timeout=1)
    assert [event.type for event in events] == [EventType.RUN_FINISHED]


@pytest.mark.asyncio
async def test_multiple_subscribers_each_receive_the_terminal_event() -> None:
    bus = EventBus()
    run_id = RunId(new_uuid7())

    async def collect() -> list[EventType]:
        return [event.type async for event in bus.events(run_id)]

    first = asyncio.create_task(collect())
    second = asyncio.create_task(collect())
    await wait_for_subscriber(bus, run_id)
    bus.publish(make_event(run_id))
    bus.publish(Event(type=EventType.RUN_FINISHED, run_id=run_id, status="completed"))
    assert await asyncio.wait_for(first, timeout=1) == [
        EventType.RUN_STARTED,
        EventType.RUN_FINISHED,
    ]
    assert await asyncio.wait_for(second, timeout=1) == [
        EventType.RUN_STARTED,
        EventType.RUN_FINISHED,
    ]


@pytest.mark.asyncio
async def test_clear_stops_subscribers() -> None:
    bus = EventBus()
    run_id = RunId(new_uuid7())

    async def collect() -> list[Event]:
        return [event async for event in bus.events(run_id)]

    task = asyncio.create_task(collect())
    await wait_for_subscriber(bus, run_id)
    bus.publish(make_event(run_id))
    bus.clear(run_id)
    events = await asyncio.wait_for(task, timeout=1)
    assert [event.type for event in events] == [EventType.RUN_STARTED]
    assert bus.history(run_id) == ()


def test_event_to_dict_has_no_secret_payload() -> None:
    run_id = RunId(new_uuid7())
    event = Event(
        type=EventType.STEP_FAILED,
        run_id=run_id,
        step_id="step-1",
        capability="probe",
        provider="ffprobe",
        duration_ms=12,
        status="failed",
        warning_codes=("provider_unavailable",),
        timestamp=datetime(2026, 10, 22, tzinfo=UTC),
    )
    payload = event.to_dict()
    assert payload["type"] == "step.failed"
    assert payload["warning_codes"] == ["provider_unavailable"]
    assert "cookie" not in payload
    assert "token" not in payload
    assert set(payload) == {
        "type",
        "run_id",
        "item_id",
        "step_id",
        "capability",
        "provider",
        "duration_ms",
        "status",
        "warning_codes",
        "sequence",
        "timestamp",
        "message",
        "progress",
        "item_position",
        "item_total",
        "item_label",
        "stage",
        "stage_index",
        "stage_total",
    }


def test_event_type_helpers() -> None:
    assert EventType.RUN_FINISHED.terminal is True
    assert EventType.STEP_STARTED.terminal is False
    assert EventType.STEP_STARTED.is_step is True
    assert EventType.RUN_STARTED.is_step is False


def test_invalid_event_fields_are_rejected() -> None:
    run_id = RunId(new_uuid7())
    with pytest.raises(ValueError):
        Event(type=EventType.RUN_STARTED, run_id=run_id, duration_ms=-1)
    with pytest.raises(ValueError):
        Event(type=EventType.RUN_STARTED, run_id=run_id, sequence=-1)
