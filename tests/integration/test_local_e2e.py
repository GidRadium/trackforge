"""End-to-end local MVP: import → library → review → re-render → doctor."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from tests.fixtures.media import DEFAULT_TAGS, LRC_TEXT, generate_media, skip_without_media_tools

from trackforge import AsyncTrackForge, RunOptions, TrackForgeConfig
from trackforge.application.dto.results import RunResult
from trackforge.application.dto.views import ReviewView

pytestmark = pytest.mark.integration


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@pytest.mark.asyncio
async def test_local_mp3_and_flac_e2e(
    tmp_path: Path,
    config: TrackForgeConfig,
) -> None:
    skip_without_media_tools()
    tags = dict(DEFAULT_TAGS)
    tags["lyrics"] = LRC_TEXT
    mp3 = generate_media(tmp_path / "media", "mp3", name="lrc.mp3", tags=tags)
    flac = generate_media(tmp_path / "media", "flac", name="track.flac")
    mp3_hash = sha256(mp3.path)
    flac_hash = sha256(flac.path)

    async with AsyncTrackForge(config) as facade:
        mp3_result = await facade.import_path(mp3.path, RunOptions(auto=True))
        assert isinstance(mp3_result, RunResult)
        assert mp3_result.status.value == "completed"
        mp3_item = mp3_result.items[0]
        assert mp3_item.status.value == "published"
        mp3_output = Path(mp3_item.output_path)
        assert mp3_output.is_file()
        assert mp3_output.suffix == ".mp3"
        assert mp3_output.with_suffix(".lrc").is_file()

        flac_result = await facade.import_path(flac.path, RunOptions(auto=True))
        assert isinstance(flac_result, RunResult)
        flac_output = Path(flac_result.items[0].output_path)
        assert flac_output.is_file()
        assert flac_output.suffix == ".flac"
        assert flac_output != mp3_output

        assert sha256(mp3.path) == mp3_hash
        assert sha256(flac.path) == flac_hash
        sidecars = list(config.library_dir.glob("trackforge*.json"))
        assert len(sidecars) >= 2
        for sidecar in sidecars:
            payload = sidecar.read_text(encoding="utf-8")
            assert "trackforge" in payload

        files_before = sorted(path.name for path in config.library_dir.iterdir())
        repeated = await facade.import_path(mp3.path, RunOptions(auto=True))
        assert isinstance(repeated, RunResult)
        assert repeated.cache_hit is True
        assert repeated.items[0].item_id == mp3_item.item_id
        assert sorted(path.name for path in config.library_dir.iterdir()) == files_before

        reviewed = await facade.review(
            mp3_item.item_id,
            action="edit",
            field="title",
            value="Locked Title",
            lock=True,
        )
        assert isinstance(reviewed, ReviewView)
        title = reviewed.field("title")
        assert title is not None
        assert title.current == "Locked Title"
        assert title.locked is True

        explanation = await facade.explain(mp3_item.item_id, "title")
        assert explanation.decision == "Locked Title"
        assert explanation.locked is True
        assert explanation.chosen_claim is not None
        assert explanation.chosen_claim.source == "user"
        assert explanation.chosen_claim.confidence == 1.0

        rendered = await facade.render(mp3_item.item_id)
        assert rendered.audio_path.is_file()
        assert rendered.bundle_hash
        published = await facade.publish(mp3_item.item_id)
        assert published.cache_hit is False
        new_path = Path(published.path)
        assert new_path.is_file()
        assert "Locked Title" in new_path.name
        assert mp3_output.is_file()

        status = await facade.status(mp3_result.run_id)
        assert status.status.value == "completed"
        assert any(step.capability_id == "publish" for step in status.steps)

        report = await facade.doctor()
        assert report.status.value in {"ok", "warning"}


@pytest.mark.asyncio
async def test_import_runs_have_steps_and_events(
    tmp_path: Path,
    config: TrackForgeConfig,
) -> None:
    skip_without_media_tools()
    fixture = generate_media(tmp_path / "media", "mp3")

    async with AsyncTrackForge(config) as facade:
        result = await facade.import_path(fixture.path, RunOptions(auto=True))
        assert isinstance(result, RunResult)
        assert {step.capability_id for step in result.steps} == {
            "plan",
            "acquire",
            "observe",
            "interpret",
            "resolve",
            "render",
            "publish",
        }
        events = [event async for event in facade.events(result.run_id)]
        types = {event.type.value for event in events}
        assert "run.started" in types
        assert "run.finished" in types
        assert any(name.startswith("step.") for name in types)
