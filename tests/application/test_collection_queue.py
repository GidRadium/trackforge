"""Collection queue ordering, pacing, pause, and cancel tests."""

from __future__ import annotations

import asyncio

import pytest

from trackforge.application.services.collection_queue import (
    CollectionQueue,
    CollectionQueueState,
)
from trackforge.domain.model.collection import CollectionItem
from trackforge.domain.model.ids import CandidateId, new_uuid7
from trackforge.domain.model.source import Candidate, LocalPath

pytestmark = pytest.mark.unit


def make_item(position: int) -> CollectionItem:
    return CollectionItem(
        position=position,
        candidate=Candidate(
            id=CandidateId(new_uuid7()),
            source_ref=LocalPath(f"/music/{position}.mp3"),
            external_id=str(position),
            title=f"Track {position}",
        ),
    )


@pytest.mark.asyncio
async def test_queue_yields_items_in_order_and_sleeps_between_them() -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    queue = CollectionQueue(
        [make_item(3), make_item(1), make_item(2)],
        min_sleep=0.5,
        sleep=fake_sleep,
    )

    seen = [item.position async for item in queue]

    assert seen == [3, 1, 2]
    assert sleeps == [0.5, 0.5]
    assert queue.state is CollectionQueueState.RUNNING


@pytest.mark.asyncio
async def test_pause_blocks_before_the_next_item_until_resume() -> None:
    queue = CollectionQueue([make_item(1), make_item(2)])
    queue.pause()
    iterator = queue.__aiter__()
    pending = asyncio.create_task(iterator.__anext__())
    await asyncio.sleep(0)
    assert pending.done() is False
    assert queue.paused is True

    queue.resume()
    first = await asyncio.wait_for(pending, timeout=1)
    assert first.position == 1

    remaining = [item async for item in iterator]
    assert [item.position for item in remaining] == [2]
    assert queue.state is CollectionQueueState.RUNNING


@pytest.mark.asyncio
async def test_pause_after_current_item_and_cancel_are_cooperative() -> None:
    queue = CollectionQueue([make_item(1), make_item(2), make_item(3)])
    seen: list[int] = []

    async def consume() -> None:
        async for item in queue:
            seen.append(item.position)
            if len(seen) in {1, 2}:
                queue.pause()

    task = asyncio.create_task(consume())
    for _ in range(50):
        if seen:
            break
        await asyncio.sleep(0.01)
    assert seen == [1]
    await asyncio.sleep(0.02)
    assert task.done() is False
    assert queue.state is CollectionQueueState.PAUSED

    queue.resume()
    for _ in range(50):
        if len(seen) == 2:
            break
        await asyncio.sleep(0.01)
    assert seen[:2] == [1, 2]
    queue.cancel()
    await asyncio.wait_for(task, timeout=1)
    assert seen == [1, 2]
    assert queue.cancelled is True


@pytest.mark.asyncio
async def test_cancel_before_start_yields_nothing() -> None:
    queue = CollectionQueue([make_item(1)])
    queue.cancel()

    assert [item async for item in queue] == []
    assert queue.cancelled is True


def test_paused_queue_does_not_pretend_to_be_cancelled() -> None:
    queue = CollectionQueue([make_item(1)])
    queue.pause()
    assert queue.checkpoint() is True
    queue.cancel()
    assert queue.checkpoint() is False
