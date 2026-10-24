"""Fake-yt-dlp end-to-end playlist processing and resume tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from tests.adapters.ytdlp.conftest import FAKE_DIR, read_log
from tests.fixtures.media import generate_media, skip_without_media_tools

from trackforge import AsyncTrackForge, CollectionPlanOptions, CollectionRunResult, RunOptions
from trackforge.application.dto.collections import CollectionPlanView
from trackforge.config import TrackForgeConfig
from trackforge.domain.model.collection import TagBasis

pytestmark = pytest.mark.integration

PLAYLIST_URL = "https://www.youtube.com/playlist?list=fixture-five"
FAILED_PLAYLIST_URL = "https://www.youtube.com/playlist?list=fixture-playlist-fail"


@pytest.fixture
def playlist_config(
    config: TrackForgeConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[TrackForgeConfig, Path]:
    monkeypatch.setenv("PATH", f"{FAKE_DIR}:{__import__('os').environ.get('PATH', '')}")
    log = tmp_path / "ytdlp-calls.jsonl"
    monkeypatch.setenv("TRACKFORGE_FAKE_YTDLP_LOG", str(log))
    return config.model_copy(update={"source_cover_enabled": False}), log


def _downloads(log: Path) -> int:
    return sum("-o" in call for call in read_log(log))


def _audio_files(directory: Path) -> list[Path]:
    return sorted(
        path
        for path in directory.iterdir()
        if path.suffix.casefold() in {".mp3", ".m4a", ".flac", ".ogg", ".opus"}
    )


@pytest.mark.asyncio
async def test_five_item_playlist_writes_subfolder_and_sidecars(
    playlist_config: tuple[TrackForgeConfig, Path],
) -> None:
    skip_without_media_tools()
    config, log = playlist_config

    async with AsyncTrackForge(config) as facade:
        result = await facade.collection_add(
            PLAYLIST_URL,
            RunOptions(auto=True),
            collection_options=CollectionPlanOptions(tag_basis=TagBasis.PLAYLIST_FIRST),
            min_sleep=0,
        )
        assert isinstance(result, CollectionRunResult)
        collection = await facade.get_collection(result.collection_id)

    assert result.status.value == "completed"
    assert result.processed == 5
    assert result.failed == 0
    assert _downloads(log) == 5
    folder = config.library_dir / "Fixture Playlist Five"
    audio = _audio_files(folder)
    assert len(audio) == 5
    assert len(list(folder.glob("trackforge*.json"))) == 5
    assert all(item.status.value == "processed" for item in collection.items)

    tracks: set[int] = set()
    for sidecar in folder.glob("trackforge*.json"):
        document = json.loads(sidecar.read_text(encoding="utf-8"))
        assert document["tag_basis"] == "playlist_first"
        assert document["collection"]["title"] == "Fixture Playlist Five"
        assert document["metadata"]["album"] == "Fixture Playlist Five"
        assert document["metadata"]["albumartist"] == "Fixture Playlist Owner"
        assert document["metadata"]["artist"] == ["Fixture Artist"]
        tracks.add(document["metadata"]["track"])
        assert "Fixture Album" in document["collection"]["alternatives"]["album"]
    assert tracks == {1, 2, 3, 4, 5}


@pytest.mark.asyncio
async def test_resume_after_two_processed_does_not_redownload_them(
    playlist_config: tuple[TrackForgeConfig, Path],
) -> None:
    skip_without_media_tools()
    config, log = playlist_config

    async with AsyncTrackForge(config) as facade:
        first = await facade.collection_add(
            PLAYLIST_URL,
            RunOptions(auto=True),
            collection_options=CollectionPlanOptions(start=1, end=2),
            min_sleep=0,
        )
        assert isinstance(first, CollectionRunResult)
        downloads_after_first = _downloads(log)
        assert downloads_after_first == 2
        assert first.processed == 2
        assert first.remaining == 3

        resumed = await facade.collection_resume(
            first.collection_id,
            RunOptions(auto=True),
            min_sleep=0,
        )
        collection = await facade.get_collection(first.collection_id)

    assert resumed.status.value == "completed"
    assert resumed.processed == 3
    assert resumed.failed == 0
    assert resumed.remaining == 0
    assert _downloads(log) == downloads_after_first + 3
    assert len(_audio_files(config.library_dir / "Fixture Playlist Five")) == 5
    assert all(item.status.value == "processed" for item in collection.items)


@pytest.mark.asyncio
async def test_one_failed_item_does_not_stop_the_collection(
    playlist_config: tuple[TrackForgeConfig, Path],
) -> None:
    skip_without_media_tools()
    config, _log = playlist_config

    async with AsyncTrackForge(config) as facade:
        result = await facade.collection_add(
            FAILED_PLAYLIST_URL,
            RunOptions(auto=True),
            collection_options=CollectionPlanOptions(),
            min_sleep=0,
        )
        assert isinstance(result, CollectionRunResult)
        collection = await facade.get_collection(result.collection_id)

    assert result.status.value == "partial"
    assert result.processed == 4
    assert result.failed == 1
    assert result.remaining == 0
    assert len(result.errors) == 1
    assert len(_audio_files(config.library_dir / "Fixture Playlist Failed")) == 4
    by_position = {item.position: item for item in collection.items}
    assert by_position[3].status.value == "failed"
    assert by_position[3].error
    assert all(by_position[position].status.value == "processed" for position in (1, 2, 4, 5))

    with sqlite3.connect(config.data_dir / "catalog.sqlite") as connection:
        statuses = dict(connection.execute("SELECT position, status FROM collection_items"))
    assert statuses[3] == "failed"


@pytest.mark.asyncio
async def test_album_first_keeps_release_metadata(
    playlist_config: tuple[TrackForgeConfig, Path],
) -> None:
    skip_without_media_tools()
    config, _log = playlist_config

    async with AsyncTrackForge(config) as facade:
        result = await facade.collection_add(
            PLAYLIST_URL,
            RunOptions(auto=True),
            collection_options=CollectionPlanOptions(
                items="1",
                tag_basis=TagBasis.ALBUM_FIRST,
            ),
            min_sleep=0,
        )
        assert isinstance(result, CollectionRunResult)

    folder = config.library_dir / "Fixture Playlist Five"
    sidecar = next(folder.glob("trackforge*.json"))
    document = json.loads(sidecar.read_text(encoding="utf-8"))
    assert document["tag_basis"] == "album_first"
    assert document["metadata"]["album"] == "Fixture Album"
    assert document["metadata"]["artist"] == ["Fixture Artist"]
    assert "Fixture Playlist Five" in document["collection"]["alternatives"]["album"]


@pytest.mark.asyncio
async def test_dry_run_enumerates_without_creating_catalog_or_downloading(
    playlist_config: tuple[TrackForgeConfig, Path],
) -> None:
    config, log = playlist_config
    facade = AsyncTrackForge(config)
    try:
        plan = await facade.collection_add(
            PLAYLIST_URL,
            RunOptions(auto=True, dry_run=True),
            collection_options=CollectionPlanOptions(),
        )
    finally:
        await facade.close()

    assert isinstance(plan, CollectionPlanView)
    assert len(plan.selected_items) == 5
    assert not config.data_dir.exists()
    assert not config.library_dir.exists()
    assert _downloads(log) == 0


@pytest.mark.asyncio
async def test_local_folder_collection_publishes_into_subfolder(
    playlist_config: tuple[TrackForgeConfig, Path],
    tmp_path: Path,
) -> None:
    skip_without_media_tools()
    config, _log = playlist_config
    folder = tmp_path / "Fixture Folder"
    generate_media(folder, "mp3", name="a.mp3")
    generate_media(folder, "mp3", name="b.mp3")

    async with AsyncTrackForge(config) as facade:
        plan = await facade.collection_plan(folder, CollectionPlanOptions())
        assert plan.collection.kind.value == "folder"
        assert [item.candidate.title for item in plan.selected_items] == ["a", "b"]

        result = await facade.collection_add(
            folder,
            RunOptions(auto=True),
            collection_options=CollectionPlanOptions(start=1, end=2),
            min_sleep=0,
        )
        assert isinstance(result, CollectionRunResult)
        collection = await facade.get_collection(result.collection_id)

    assert result.status.value == "completed"
    assert result.processed == 2
    assert collection.kind.value == "folder"
    audio = _audio_files(config.library_dir / "Fixture Folder")
    assert len(audio) == 2
    assert len(list((config.library_dir / "Fixture Folder").glob("trackforge*.json"))) == 2
