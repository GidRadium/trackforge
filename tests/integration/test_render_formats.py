"""End-to-end render tests for all five MVP output formats."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.fixtures.media import generate_media, generate_png, skip_without_media_tools

from trackforge.adapters.tagging.mutagen import MutagenTagReader
from trackforge.application.dto.plan import AcquireOptions, ItemView, PlanOptions
from trackforge.application.dto.rendering import CoverArt, RenderOptions
from trackforge.bootstrap.container import CatalogContext, build_catalog_context
from trackforge.config import TrackForgeConfig
from trackforge.domain.model.outputs import TargetFormat
from trackforge.domain.model.source import LocalPath

pytestmark = pytest.mark.integration


async def _resolve_fixture(context: CatalogContext, source: Path) -> ItemView:
    plan = await context.plan_item().plan(
        LocalPath(str(source)),
        PlanOptions(),
        context.source_resolvers(),
    )
    assert plan.blocked_reason is None
    acquired = await context.acquire_item().acquire(
        plan.candidates[0],
        AcquireOptions(),
        context.acquirers(),
    )
    await context.observe_item().observe(acquired.item_id, "balanced")
    await context.interpret_item().interpret(acquired.item_id)
    async with context.unit_of_work() as uow:
        await uow.locks.upsert(
            acquired.item_id,
            "mb_recording_id",
            "11111111-1111-1111-1111-111111111111",
        )
    await context.resolve_item().resolve(acquired.item_id)
    return acquired


@pytest.mark.parametrize("container", ("mp3", "m4a", "flac", "ogg", "opus"))
@pytest.mark.asyncio
async def test_render_each_source_format(
    tmp_path: Path,
    config: TrackForgeConfig,
    container: str,
) -> None:
    skip_without_media_tools()
    fixture = generate_media(tmp_path / "media", container, duration=0.3)
    original_hash = hashlib.sha256(fixture.path.read_bytes()).hexdigest()
    cover = CoverArt.from_path(generate_png(tmp_path / "covers"))

    with build_catalog_context(config) as context:
        acquired = await _resolve_fixture(context, fixture.path)
        result = await context.render_item().render(
            acquired.item_id,
            RenderOptions(),
            cover=cover,
        )

        audio = result.directory / f"track.{container}"
        assert audio.is_file()
        assert (result.directory / "trackforge.json").is_file()
        tags = MutagenTagReader().read(audio)
        assert tags.title == "Fixture Song"
        assert tags.artists == ("Fixture Artist",)
        assert tags.album == "Fixture Album"
        assert tags.date == "2024"
        assert tags.track == 3
        assert tags.genre == ("Test Genre",)
        assert tags.lyrics == "la la la"
        assert tags.mb_recording_id == "11111111-1111-1111-1111-111111111111"
        embedded = MutagenTagReader().read_cover(audio)
        assert embedded is not None
        assert embedded.data == cover.data

        sidecar = json.loads((result.directory / "trackforge.json").read_text(encoding="utf-8"))
        assert sidecar["schema_version"] == 1
        assert sidecar["trackforge"]["content_hash"] == original_hash
        assert sidecar["trackforge"]["bundle_hash"] == result.bundle.bundle_hash
        assert sidecar["metadata"]["title"] == "Fixture Song"
        assert sidecar["source"]["extractor"] == "local"
        assert sidecar["warnings"] == []
        assert audio.name in result.bundle.files
        assert "trackforge.json" in result.bundle.files

    assert hashlib.sha256(fixture.path.read_bytes()).hexdigest() == original_hash


@pytest.mark.asyncio
async def test_render_transcodes_to_requested_format(
    tmp_path: Path,
    config: TrackForgeConfig,
) -> None:
    skip_without_media_tools()
    fixture = generate_media(tmp_path / "media", "mp3", duration=0.3)
    cover = CoverArt.from_path(generate_png(tmp_path / "covers"))

    with build_catalog_context(config) as context:
        acquired = await _resolve_fixture(context, fixture.path)
        result = await context.render_item().render(
            acquired.item_id,
            RenderOptions(target_format=TargetFormat.FLAC),
            cover=cover,
        )

        audio = result.directory / "track.flac"
        assert audio.is_file()
        assert result.render.target_format is TargetFormat.FLAC
        assert MutagenTagReader().read(audio).title == "Fixture Song"
        assert MutagenTagReader().read_cover(audio) is not None


@pytest.mark.asyncio
async def test_renderer_falls_back_to_flac_for_unsupported_source(
    tmp_path: Path,
    config: TrackForgeConfig,
) -> None:
    skip_without_media_tools()
    import struct
    import wave

    from trackforge.adapters.rendering.ffmpeg import FfmpegRenderer
    from trackforge.domain.model.ids import MediaId
    from trackforge.domain.model.media import MediaContainer, MediaObject

    wav_path = tmp_path / "source.wav"
    # A short silent WAV is enough for ffmpeg to validate the stream.
    with wave.open(str(wav_path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(struct.pack("<h", 0) * 800)
    payload = wav_path.read_bytes()
    media = MediaObject(
        id=MediaId("media-1"),
        content_hash=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        cas_path=str(wav_path),
        container=MediaContainer.WAV,
    )
    result = FfmpegRenderer().render(
        media,
        None,  # type: ignore[arg-type]
        RenderOptions(),
        tmp_path / "out",
    )
    assert result.path.name == "track.flac"
    assert result.format is TargetFormat.FLAC
    assert result.path.is_file()


@pytest.mark.parametrize("container", ("ogg", "opus"))
@pytest.mark.asyncio
async def test_bundle_hash_is_stable_for_nondeterministic_containers(
    tmp_path: Path,
    config: TrackForgeConfig,
    container: str,
) -> None:
    skip_without_media_tools()
    fixture = generate_media(tmp_path / "media", container, duration=0.3)
    cover = CoverArt.from_path(generate_png(tmp_path / "covers"))

    with build_catalog_context(config) as context:
        acquired = await _resolve_fixture(context, fixture.path)
        fixed_time = datetime.now(UTC)
        first = await context.render_item().render(
            acquired.item_id,
            cover=cover,
            render_id="01900000-0000-7000-8000-000000000101",
            bundle_id="01900000-0000-7000-8000-000000000102",
            created_at=fixed_time,
        )
        second = await context.render_item().render(
            acquired.item_id,
            cover=cover,
            render_id="01900000-0000-7000-8000-000000000201",
            bundle_id="01900000-0000-7000-8000-000000000202",
            created_at=fixed_time,
        )
    assert first.bundle.bundle_hash == second.bundle.bundle_hash
    assert first.bundle.bundle_hash != ""
