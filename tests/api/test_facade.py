"""Public Python API tests for the synchronous and async facades."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from tests.fixtures.media import generate_media, skip_without_media_tools

from trackforge import AsyncTrackForge, RunOptions, TrackForge, TrackForgeConfig
from trackforge.application.dto.plan import PlanView
from trackforge.application.dto.results import RunResult


def test_public_exports_are_importable() -> None:
    assert TrackForge is not None
    assert AsyncTrackForge is not None
    assert TrackForgeConfig is not None
    assert RunOptions is not None


def test_sync_facade_import_plan_explain_and_doctor(
    tmp_path: Path,
    config: TrackForgeConfig,
) -> None:
    skip_without_media_tools()
    fixture = generate_media(tmp_path / "media", "mp3")

    with TrackForge(config) as facade:
        plan = facade.plan(fixture.path)
        assert isinstance(plan, PlanView)
        assert plan.candidates

        result = facade.import_path(fixture.path, RunOptions(auto=True))
        assert isinstance(result, RunResult)
        assert result.status.value == "completed"
        assert result.output_paths
        assert Path(result.output_paths[0]).is_file()

        status = facade.status(result.run_id)
        assert status.status.value == "completed"
        assert status.steps

        explanation = facade.explain(result.items[0].item_id, "title")
        assert explanation.decision == "Fixture Song"

        report = facade.doctor()
        assert report.status.value in {"ok", "warning"}


def test_async_facade_context_and_events(
    tmp_path: Path,
    config: TrackForgeConfig,
) -> None:
    skip_without_media_tools()
    fixture = generate_media(tmp_path / "media", "mp3")

    async def scenario() -> None:
        async with AsyncTrackForge(config) as facade:
            result = await facade.import_path(fixture.path, RunOptions(auto=True))
            assert isinstance(result, RunResult)
            events = [event async for event in facade.events(result.run_id)]
            assert events
            assert any(event.type.value == "run.finished" for event in events)

    asyncio.run(scenario())


def test_sync_facade_rejects_active_event_loop(
    tmp_path: Path,
    config: TrackForgeConfig,
) -> None:
    skip_without_media_tools()
    fixture = generate_media(tmp_path / "media", "mp3")

    async def scenario() -> None:
        facade = TrackForge(config)
        with pytest.raises(RuntimeError, match="AsyncTrackForge"):
            facade.plan(fixture.path)
        with pytest.raises(RuntimeError, match="AsyncTrackForge"):
            facade.import_path(fixture.path)

    asyncio.run(scenario())


def test_plan_does_not_open_or_create_data_dir(
    tmp_path: Path,
    config: TrackForgeConfig,
) -> None:
    skip_without_media_tools()
    fixture = generate_media(tmp_path / "media", "mp3")

    async def scenario() -> None:
        facade = AsyncTrackForge(config)
        plan = await facade.plan(fixture.path)
        assert isinstance(plan, PlanView)
        await facade.close()
        assert not config.data_dir.exists() or not any(config.data_dir.iterdir())

    asyncio.run(scenario())
