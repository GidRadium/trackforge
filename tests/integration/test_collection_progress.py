"""End-to-end collection progress/board/report tests with fake yt-dlp."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.adapters.ytdlp.conftest import FAKE_DIR
from tests.fixtures.media import skip_without_media_tools

from trackforge import AsyncTrackForge, CollectionPlanOptions, CollectionRunResult, RunOptions
from trackforge.application.dto.progress import (
    CollectionProgressStatus,
    HandlerStatus,
    ProgressStatus,
)
from trackforge.config import TrackForgeConfig

pytestmark = pytest.mark.integration

PLAYLIST_URL = "https://www.youtube.com/playlist?list=fixture-five"
FAILED_PLAYLIST_URL = "https://www.youtube.com/playlist?list=fixture-playlist-fail"


@pytest.fixture
def progress_config(
    config: TrackForgeConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> TrackForgeConfig:
    monkeypatch.setenv("PATH", f"{FAKE_DIR}:{__import__('os').environ.get('PATH', '')}")
    monkeypatch.setenv("TRACKFORGE_FAKE_YTDLP_LOG", str(tmp_path / "ytdlp-progress.jsonl"))
    return config.model_copy(update={"source_cover_enabled": False})


@pytest.mark.asyncio
async def test_two_item_collection_progress_and_handler_reports(
    progress_config: TrackForgeConfig,
) -> None:
    skip_without_media_tools()

    async with AsyncTrackForge(progress_config) as facade:
        result = await facade.collection_add(
            PLAYLIST_URL,
            RunOptions(auto=True),
            collection_options=CollectionPlanOptions(items="1-2"),
            min_sleep=0,
        )
        assert isinstance(result, CollectionRunResult)
        snapshot = await facade.collection_progress(result.collection_id)
        board_snapshot = facade.progress_board.snapshot(str(result.collection_id))
        reports = await facade.collection_handler_reports(result.collection_id)

    assert snapshot == board_snapshot
    assert snapshot.status is CollectionProgressStatus.COMPLETED
    assert snapshot.total == 2
    assert snapshot.completed == 2
    assert snapshot.remaining == 0
    assert [item.status for item in snapshot.items] == [
        ProgressStatus.DONE,
        ProgressStatus.DONE,
    ]
    assert all(item.label for item in snapshot.items)
    assert all(item.output_path for item in snapshot.items)
    assert [item.position for item in snapshot.items] == [1, 2]

    capabilities = {report.capability_id for report in reports}
    assert {"acquire", "observe", "resolve", "render", "publish"} <= capabilities
    assert "ffprobe" in capabilities
    assert all(report.status is not HandlerStatus.FAILED for report in reports)
    assert any(report.duration_ms >= 0 for report in reports)
    assert any(report.summary for report in reports)


@pytest.mark.asyncio
async def test_failed_item_is_visible_without_stopping_others(
    progress_config: TrackForgeConfig,
) -> None:
    skip_without_media_tools()

    async with AsyncTrackForge(progress_config) as facade:
        result = await facade.collection_add(
            FAILED_PLAYLIST_URL,
            RunOptions(auto=True),
            collection_options=CollectionPlanOptions(items="1-3"),
            min_sleep=0,
        )
        assert isinstance(result, CollectionRunResult)
        snapshot = await facade.collection_progress(result.collection_id)

    assert snapshot.status is CollectionProgressStatus.PARTIAL
    assert snapshot.completed == 2
    assert snapshot.failed == 1
    assert snapshot.remaining == 0
    assert [item.status for item in snapshot.items] == [
        ProgressStatus.DONE,
        ProgressStatus.DONE,
        ProgressStatus.FAILED,
    ]
    assert snapshot.items[2].message
