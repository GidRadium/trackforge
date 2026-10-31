"""End-to-end metadata quality for a noisy fake-yt-dlp playlist."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import httpx
import pytest
import respx
from tests.adapters.ytdlp.conftest import FAKE_DIR
from tests.fixtures.media import generate_png, skip_without_media_tools

from trackforge import AsyncTrackForge, CollectionPlanOptions, CollectionRunResult, RunOptions
from trackforge.config import TrackForgeConfig
from trackforge.domain.model.collection import TagBasis

pytestmark = pytest.mark.integration

PLAYLIST_URL = "https://www.youtube.com/playlist?list=fixture-noisy-metadata"
COVER_URL = "https://i.ytimg.com/vi/noisy-playlist/hqdefault.jpg"


@pytest.fixture
def noisy_playlist_config(
    config: TrackForgeConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> TrackForgeConfig:
    monkeypatch.setenv("PATH", f"{FAKE_DIR}:{os.environ.get('PATH', '')}")
    log = tmp_path / "ytdlp-calls.jsonl"
    monkeypatch.setenv("TRACKFORGE_FAKE_YTDLP_LOG", str(log))
    return config.model_copy(
        update={
            "source_cover_enabled": True,
            "metadata_enabled": False,
            "acoustid_api_key": "",
            "musicbrainz_user_agent": "",
        }
    )


def _audio_files(directory: Path) -> list[Path]:
    return sorted(
        path
        for path in directory.iterdir()
        if path.suffix.casefold() in {".mp3", ".m4a", ".flac", ".ogg", ".opus"}
    )


def _sidecars(directory: Path) -> list[dict]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob("trackforge*.json"))
    ]


@pytest.mark.asyncio
async def test_noisy_playlist_has_clean_derivative_metadata_and_lyrics(
    noisy_playlist_config: TrackForgeConfig,
    tmp_path: Path,
) -> None:
    skip_without_media_tools()
    config = noisy_playlist_config
    playlist_cover = generate_png(
        tmp_path / "cover", name="playlist.png", width=480, height=360
    ).read_bytes()
    first_cover = generate_png(
        tmp_path / "cover", name="first.png", width=481, height=360
    ).read_bytes()
    second_cover = generate_png(
        tmp_path / "cover", name="second.png", width=482, height=360
    ).read_bytes()

    def cover_response(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == COVER_URL:
            content = playlist_cover
        elif "noisy-one" in url:
            content = first_cover
        elif "noisy-two" in url:
            content = second_cover
        else:
            return httpx.Response(404, text="not found")
        return httpx.Response(
            200,
            content=content,
            headers={"Content-Type": "image/png"},
        )

    with respx.mock:
        respx.route(url__regex=r"https://(?:i\.ytimg\.com|img\.youtube\.com)/vi/.*").mock(
            side_effect=cover_response
        )
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
    assert result.processed == 2
    assert result.failed == 0
    folder = config.library_dir / "Fixture Noisy Playlist"
    audio = _audio_files(folder)
    assert [path.name for path in audio] == [
        "Another Artist - Another Song.m4a",
        "Rock Goes Nightcore feat. End Of The Dream - Collide (Nightcore).m4a",
    ]
    assert all("{" not in path.name and "with lyrics" not in path.name for path in audio)

    document = next(
        sidecar for sidecar in _sidecars(folder) if sidecar["metadata"]["title"] == "Collide"
    )
    metadata = document["metadata"]
    assert metadata["title"] == "Collide"
    assert metadata["artist"] == ["Rock Goes Nightcore", "End Of The Dream"]
    assert metadata["remixer"] == "Rock Goes Nightcore"
    assert metadata["originalartist"] == "End Of The Dream"
    assert metadata["originaltitle"] == (
        "{642} Nightcore (End Of The Dream) - Collide (with lyrics)"
    )
    assert metadata["qualifier"] == "Nightcore"
    assert metadata["comment"] == ("Nightcore by Rock Goes Nightcore; original by End Of The Dream")
    assert metadata["album"] == "Fixture Noisy Playlist"
    assert metadata["lyrics"] and "paper moon" in metadata["lyrics"]
    assert len(metadata["genre"]) <= 6
    assert all("Website Category" not in genre for genre in metadata["genre"])
    assert metadata["artwork"] and len(metadata["artwork"]) == 64
    second_document = next(
        sidecar for sidecar in _sidecars(folder) if sidecar["metadata"]["title"] == "Another Song"
    )
    item_artwork = {metadata["artwork"], second_document["metadata"]["artwork"]}
    assert len(item_artwork) == 2
    assert hashlib.sha256(playlist_cover).hexdigest() not in item_artwork
    first_item = next(item for item in result.items if item.position == 1)
    assert set(document["warnings"]) == set(first_item.warnings)
    assert not any(
        "album is unknown" in warning or "albumartist is unknown" in warning
        for warning in document["warnings"]
    )
    assert not any("path_unavailable" in warning for warning in result.warnings)

    assert second_document["metadata"]["artist"] == ["Another Artist"]
    assert "{" not in second_document["metadata"]["title"]
    assert "with lyrics" not in second_document["metadata"]["title"]

    assert all(item.status.value == "processed" for item in collection.items)
