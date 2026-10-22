"""Tests for read-only initial tag observation."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest
from tests.fixtures.media import (
    CONTAINER_CODECS,
    MBID_RECORDING,
    generate_media,
    skip_without_media_tools,
)

from trackforge.adapters.tagging.mutagen import reader as reader_module
from trackforge.adapters.tagging.mutagen.reader import InitialTagsObserver
from trackforge.application.dto.observations import InitialTagsPayload
from trackforge.application.ports.execution import (
    Capability,
    StepContext,
    StepInput,
    StepResultStatus,
)
from trackforge.application.ports.observations import MEDIA_ARTIFACT
from trackforge.application.services.canonical_json import canonical_json, sha256_hex
from trackforge.domain.model.capabilities import Determinism
from trackforge.domain.model.ids import ItemId, RunId, StepId, new_uuid7

pytestmark = pytest.mark.integration


def media_step(path: Path, container: str, *, data: bytes | None = None) -> StepInput:
    raw = path.read_bytes() if data is None else data
    descriptor = {
        "media_id": new_uuid7(),
        "content_hash": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
        "cas_path": str(path),
        "container": container,
    }
    payload = canonical_json(descriptor)
    return StepInput(
        type=MEDIA_ARTIFACT,
        artifact_id=new_uuid7(),
        schema_version=1,
        cache_key=descriptor["content_hash"],
        payload_hash=sha256_hex(payload),
        payload=payload,
    )


def step_context(media: StepInput) -> StepContext:
    return StepContext(
        run_id=RunId(new_uuid7()),
        item_id=ItemId(new_uuid7()),
        step_id=StepId(new_uuid7()),
        inputs={MEDIA_ARTIFACT: media},
    )


def test_initial_tags_satisfies_capability_protocol() -> None:
    capability = InitialTagsObserver()
    assert isinstance(capability, Capability)
    assert capability.manifest.cacheable is True
    assert capability.manifest.determinism is Determinism.DETERMINISTIC
    assert capability.manifest.license == "GPL-2.0-or-later"


@pytest.mark.asyncio
@pytest.mark.parametrize("container", sorted(CONTAINER_CODECS))
async def test_reads_existing_tags_without_modifying_file(
    tmp_path: Path,
    container: str,
) -> None:
    skip_without_media_tools()
    fixture = generate_media(tmp_path, container)
    before = (
        fixture.path.stat().st_mtime_ns,
        hashlib.sha256(fixture.path.read_bytes()).hexdigest(),
    )

    result = await InitialTagsObserver().run(step_context(media_step(fixture.path, container)))

    after = (fixture.path.stat().st_mtime_ns, hashlib.sha256(fixture.path.read_bytes()).hexdigest())
    assert after == before
    assert result.status is StepResultStatus.SUCCEEDED
    payload = InitialTagsPayload.model_validate(result.artifacts[0].payload)
    assert payload.tags.title == "Fixture Song"
    assert payload.tags.artist == ["Fixture Artist"]
    assert payload.tags.album == "Fixture Album"
    assert payload.tags.albumartist == ["Fixture Album Artist"]
    assert payload.tags.date == "2024"
    assert payload.tags.genre == ["Test Genre"]
    assert payload.tags.track == 3
    assert payload.tags.track_total == 12
    assert payload.tags.disc == 1
    assert payload.tags.disc_total == 2
    assert payload.tags.comment == "fixture comment"
    assert payload.tags.lyrics == "la la la"
    if container != "m4a":
        assert payload.tags.mbids["recording"] == [MBID_RECORDING]
    assert payload.raw.format
    assert payload.raw.tag_keys
    assert payload.reader.id == "mutagen"
    assert payload.reader.version
    assert payload.tags.artwork == []


@pytest.mark.asyncio
async def test_missing_tags_produce_an_empty_normalized_view(tmp_path: Path) -> None:
    skip_without_media_tools()
    fixture = generate_media(tmp_path, "flac", with_tags=False)

    result = await InitialTagsObserver().run(step_context(media_step(fixture.path, "flac")))

    payload = InitialTagsPayload.model_validate(result.artifacts[0].payload)
    assert payload.tags.title is None
    assert payload.tags.artist == []
    assert payload.tags.album is None
    assert payload.tags.mbids == {}


@pytest.mark.asyncio
async def test_missing_mutagen_is_a_visible_skip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "song.mp3"
    path.write_bytes(b"not audio")
    monkeypatch.setattr(reader_module, "load_mutagen", lambda: None)

    result = await InitialTagsObserver().run(step_context(media_step(path, "mp3")))

    assert result.status is StepResultStatus.SKIPPED
    assert result.artifacts == ()
    assert [warning.code for warning in result.warnings] == ["tag_reader_missing"]


@pytest.mark.asyncio
async def test_unreadable_tags_degrade_with_warning(tmp_path: Path) -> None:
    path = tmp_path / "broken.flac"
    path.write_bytes(b"this is not a flac file")

    result = await InitialTagsObserver().run(step_context(media_step(path, "flac")))

    assert result.status is StepResultStatus.SKIPPED
    assert [warning.code for warning in result.warnings] == ["tag_reader_failed"]


@pytest.mark.asyncio
async def test_embedded_artwork_is_described_without_bytes(tmp_path: Path) -> None:
    skip_without_media_tools()
    fixture = generate_media(tmp_path, "mp3")
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    cover = tmp_path / "cover.png"
    subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=64x64:d=0.1",
            "-frames:v",
            "1",
            str(cover),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    tagged = tmp_path / "with-cover.mp3"
    subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-i",
            str(fixture.path),
            "-i",
            str(cover),
            "-map",
            "0:a",
            "-map",
            "1:v",
            "-c:a",
            "copy",
            "-c:v",
            "png",
            "-id3v2_version",
            "3",
            "-metadata:s:v",
            "title=Album cover",
            "-metadata:s:v",
            "comment=Cover (front)",
            str(tagged),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )

    result = await InitialTagsObserver().run(step_context(media_step(tagged, "mp3")))

    payload = InitialTagsPayload.model_validate(result.artifacts[0].payload)
    assert len(payload.tags.artwork) == 1
    image = payload.tags.artwork[0]
    assert image.mime_type == "image/png"
    assert image.description == "Album cover"
    assert image.size_bytes is not None and image.size_bytes > 0
    assert "data" not in payload.model_dump()["tags"]["artwork"][0]
