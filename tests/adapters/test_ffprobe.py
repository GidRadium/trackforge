"""Unit and tool-backed tests for the ffprobe capability."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from tests.fixtures.media import generate_media, skip_without_media_tools

from trackforge.adapters.analysis.ffprobe import FfprobeProbe, parse_ffprobe_document
from trackforge.application.dto.observations import AudioProbePayload
from trackforge.application.ports.execution import Capability, StepContext, StepInput, StepResult
from trackforge.application.ports.observations import MEDIA_ARTIFACT
from trackforge.application.services.canonical_json import canonical_json, sha256_hex
from trackforge.domain.errors import ProbeFailed, ProviderUnavailable
from trackforge.domain.model.ids import ItemId, RunId, StepId, new_uuid7

pytestmark = pytest.mark.unit

SAMPLE = {
    "format": {
        "format_name": "mp3",
        "format_long_name": "MP2/3 (MPEG audio layer 2/3)",
        "duration": "12.345",
        "size": "98765",
        "bit_rate": "64000",
    },
    "streams": [
        {
            "index": 0,
            "codec_type": "audio",
            "codec_name": "mp3",
            "sample_rate": "44100",
            "channels": 2,
            "bit_rate": "64000",
            "duration": "12.3",
            "channel_layout": "stereo",
        },
        {
            "index": 1,
            "codec_type": "video",
            "codec_name": "mjpeg",
            "width": 600,
            "height": 600,
            "disposition": {"attached_pic": 1},
            "tags": {"title": "Cover", "comment": "front"},
        },
    ],
    "chapters": [
        {
            "id": "1",
            "start_time": "0.0",
            "end_time": "5.0",
            "tags": {"title": "Intro"},
        }
    ],
}


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


def step_context(media: StepInput, params: dict[str, Any] | None = None) -> StepContext:
    return StepContext(
        run_id=RunId(new_uuid7()),
        item_id=ItemId(new_uuid7()),
        step_id=StepId(new_uuid7()),
        inputs={MEDIA_ARTIFACT: media},
        params=params or {},
    )


def test_probe_satisfies_capability_protocol() -> None:
    assert isinstance(FfprobeProbe(), Capability)
    assert FfprobeProbe().manifest.id == "ffprobe"


def test_parse_normalizes_format_streams_chapters_and_pictures() -> None:
    payload = parse_ffprobe_document(SAMPLE, container="mp3")

    assert payload.container == "mp3"
    assert payload.format_name == "mp3"
    assert payload.duration_seconds == pytest.approx(12.345)
    assert payload.size_bytes == 98765
    assert payload.bit_rate == 64000
    assert payload.streams[0].codec_name == "mp3"
    assert payload.streams[0].sample_rate == 44100
    assert payload.streams[0].channels == 2
    assert payload.streams[0].duration_seconds == pytest.approx(12.3)
    assert payload.chapters[0].title == "Intro"
    assert payload.chapters[0].end_seconds == pytest.approx(5.0)
    assert payload.attached_pictures[0].codec_name == "mjpeg"
    assert payload.attached_pictures[0].width == 600
    assert payload.first_audio_stream() is payload.streams[0]


def test_parse_rejects_missing_format_information() -> None:
    with pytest.raises(ProbeFailed):
        parse_ffprobe_document({"streams": []})


def test_parse_derives_container_from_format_name() -> None:
    payload = parse_ffprobe_document({"format": {"format_name": "flac"}})
    assert payload.container == "flac"
    assert payload.streams == []


@pytest.mark.asyncio
async def test_probe_runs_ffprobe_on_real_media(tmp_path: Path) -> None:
    skip_without_media_tools()
    fixture = generate_media(tmp_path, "mp3")
    result = await FfprobeProbe().run(step_context(media_step(fixture.path, "mp3")))

    assert isinstance(result, StepResult)
    payload = AudioProbePayload.model_validate(result.artifacts[0].payload)
    assert payload.container == "mp3"
    assert payload.duration_seconds is not None
    assert payload.duration_seconds > 0
    assert payload.first_audio_stream() is not None
    assert payload.first_audio_stream().codec_name == "mp3"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_missing_ffprobe_is_a_typed_non_retryable_error(tmp_path: Path) -> None:
    path = tmp_path / "fake.mp3"
    path.write_bytes(b"not audio")
    probe = FfprobeProbe(executable="trackforge-definitely-missing-ffprobe")

    with pytest.raises(ProviderUnavailable) as caught:
        await probe.run(step_context(media_step(path, "mp3")))
    assert caught.value.retryable is False
    assert caught.value.user_action is not None


@pytest.mark.asyncio
async def test_corrupt_file_is_a_probe_failure(tmp_path: Path) -> None:
    skip_without_media_tools()
    path = tmp_path / "corrupt.mp3"
    path.write_bytes(b"this is not an mp3 file")
    with pytest.raises(ProbeFailed) as caught:
        await FfprobeProbe().run(step_context(media_step(path, "mp3")))
    assert caught.value.retryable is False
    assert "stderr" in caught.value.context


@pytest.mark.asyncio
async def test_missing_media_bytes_are_reported(tmp_path: Path) -> None:
    missing = tmp_path / "missing.mp3"
    with pytest.raises(ProbeFailed):
        await FfprobeProbe().run(step_context(media_step(missing, "mp3", data=b"deadbeef")))
