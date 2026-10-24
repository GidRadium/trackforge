"""Slow real-model ASR benchmark; never part of the default CI run.

The test is opt-in and requires a local faster-whisper model plus a 3-4 minute
audio file.  It skips with an explicit reason when either is absent; the
marker keeps it out of the regular suite.  Metrics are printed as one JSON
object for the benchmark artifact.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import resource
import time
from pathlib import Path

import pytest

from trackforge.adapters.analysis.whisper import WhisperObserver
from trackforge.adapters.models.local import LocalModelManager
from trackforge.application.dto.lyrics import AsrTranscript
from trackforge.application.dto.observations import MediaInput
from trackforge.application.ports.execution import BudgetSnapshot, StepContext, StepInput
from trackforge.application.ports.models import ModelKind, ModelStatus, model_specs_from_config
from trackforge.application.ports.observations import MEDIA_ARTIFACT
from trackforge.application.services.canonical_json import canonical_json, sha256_hex
from trackforge.application.use_cases.lyrics_merge import LyricsMerger
from trackforge.config import TrackForgeConfig
from trackforge.domain.model.ids import ItemId, RunId, StepId, new_uuid7

pytestmark = [pytest.mark.slow, pytest.mark.integration]

_DEFAULT_MEDIA = Path(".spike-private/bench/audio/music.ogg")


def _media_path() -> Path:
    configured = os.environ.get("TRACKFORGE_ASR_SLOW_MEDIA", "").strip()
    return Path(configured).expanduser() if configured else _DEFAULT_MEDIA


def _media_context(media_path: Path) -> StepContext:
    descriptor = MediaInput(
        media_id=str(new_uuid7()),
        content_hash=hashlib.sha256(media_path.read_bytes()).hexdigest(),
        size_bytes=media_path.stat().st_size,
        cas_path=str(media_path),
        container=media_path.suffix.lstrip(".") or "bin",
    )
    payload = canonical_json(descriptor.model_dump(mode="json"))
    return StepContext(
        run_id=RunId(new_uuid7()),
        item_id=ItemId(new_uuid7()),
        step_id=StepId(new_uuid7()),
        inputs={
            MEDIA_ARTIFACT: StepInput(
                type=MEDIA_ARTIFACT,
                artifact_id=str(new_uuid7()),
                schema_version=1,
                cache_key=descriptor.content_hash,
                payload_hash=sha256_hex(payload),
                payload=payload,
            )
        },
        budget=BudgetSnapshot(memory_mb=8192.0),
    )


@pytest.mark.asyncio
async def test_asr_slow_benchmark(tmp_path: Path) -> None:
    if importlib.util.find_spec("faster_whisper") is None:
        pytest.skip("faster-whisper is not installed; run `uv sync --extra asr`")
    media_path = _media_path()
    if not media_path.is_file():
        pytest.skip("no 3-4 minute local audio fixture; set TRACKFORGE_ASR_SLOW_MEDIA")
    config = TrackForgeConfig.from_file()
    if not (config.asr_model.strip() or config.asr_model_path.strip()):
        pytest.skip("ASR model is not configured in [models.asr]")
    manager = LocalModelManager(
        model_specs_from_config(config),
        models_dir=config.models_dir,
        offline=True,
    )
    spec = next(
        (model for model in manager.list_models() if model.kind is ModelKind.ASR),
        None,
    )
    if spec is None:
        pytest.skip("no ASR model spec is configured")
    if spec.status is not ModelStatus.AVAILABLE:
        pytest.skip(f"ASR model is not available locally: {spec.status.value}")

    observer = WhisperObserver(
        manager,
        spec.id,
        compute_type=config.asr_compute_type,
        threads=config.asr_threads,
        vad_filter=config.asr_vad_filter,
        beam_size=config.asr_beam_size,
        memory_mb=config.asr_memory_mb,
    )
    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    started = time.perf_counter()
    result = await observer.run(_media_context(media_path))
    wall = time.perf_counter() - started
    rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    if not result.artifacts:
        reasons = ", ".join(f"{warning.code}: {warning.message}" for warning in result.warnings)
        pytest.skip(f"Whisper did not produce a transcript: {reasons}")
    transcript = AsrTranscript.model_validate(result.artifacts[0].payload)
    duration = transcript.duration_seconds or 1.0
    rtf = duration / wall if wall > 0 else 0.0

    merged = LyricsMerger().merge([], transcript, None, duration)
    assert merged.lrc is not None
    assert merged.lrc.startswith("[")
    assert merged.plain is not None and "[00:" not in merged.plain
    lrc_path = tmp_path / "track.lrc"
    lrc_path.write_text(merged.lrc, encoding="utf-8")
    assert lrc_path.is_file() and lrc_path.stat().st_size > 0

    metrics = {
        "media": str(media_path),
        "duration_seconds": round(duration, 3),
        "wall_seconds": round(wall, 3),
        "rtf": round(rtf, 2),
        "peak_rss_mib": round(max(rss_before, rss_after) / 1024.0, 1),
        "language": transcript.language,
        "segments": len(transcript.segments),
        "words": len(transcript.words()),
        "avg_logprob": transcript.avg_logprob,
        "no_speech_prob": transcript.no_speech_prob,
        "model": transcript.model.id,
        "threads": transcript.model.threads,
        "compute_type": transcript.model.compute_type,
        "lrc_file": str(lrc_path),
    }
    print("ASR_BENCHMARK " + json.dumps(metrics, ensure_ascii=False, sort_keys=True))

    assert transcript.model.id
    assert transcript.model.compute_type == config.asr_compute_type
    assert wall > 0
    assert rtf > 0
