"""Slow Demucs benchmark on one real track (never part of the default suite).

Run explicitly with a local checkpoint and a 3–4 minute audio file:

    TRACKFORGE_DEMUCS_AUDIO=/path/track.flac \
    TRACKFORGE_DEMUCS_MODEL=/path/955717e8-8726e21a.th \
    uv run pytest -m slow tests/integration/test_demucs_slow.py -q -s

Set ``TRACKFORGE_DEMUCS_BENCH_OUT`` to write the measured time/RAM JSON.
The test never downloads weights: the checkpoint must already be local.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import resource
import time
from pathlib import Path
from typing import Any

import pytest

from trackforge.adapters.analysis.demucs import DemucsObserver
from trackforge.adapters.models.local import LocalModelManager
from trackforge.application.dto.observations import (
    AudioProbePayload,
    InitialTagsPayload,
    MediaInput,
    PathStructurePayload,
    RawTagInfo,
    TagReaderInfo,
)
from trackforge.application.dto.stems import StemsPayload
from trackforge.application.ports.execution import (
    BudgetSnapshot,
    StepContext,
    StepInput,
    StepResultStatus,
)
from trackforge.application.ports.models import ModelInfo, ModelKind
from trackforge.application.ports.observations import (
    MEDIA_ARTIFACT,
    PROBE_ARTIFACT,
    STEMS_ARTIFACT,
)
from trackforge.application.services.canonical_json import canonical_json, sha256_hex
from trackforge.application.use_cases.interpret_item import (
    ArtifactPayload,
    InterpreterInput,
    derivative_detector,
)
from trackforge.domain.model.ids import ItemId, RunId, StepId, new_uuid7

pytestmark = [pytest.mark.slow, pytest.mark.integration]

AUDIO = os.environ.get("TRACKFORGE_DEMUCS_AUDIO", "").strip()
MODEL = os.environ.get("TRACKFORGE_DEMUCS_MODEL", "").strip()


def _rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def _context(audio: Path) -> StepContext:
    digest = hashlib.sha256(audio.read_bytes()).hexdigest()
    descriptor = MediaInput(
        media_id=str(new_uuid7()),
        content_hash=digest,
        size_bytes=audio.stat().st_size,
        cas_path=str(audio),
        container=audio.suffix.lstrip(".") or "audio",
    )
    payload = canonical_json(descriptor.model_dump(mode="json"))
    step_input = StepInput(
        type=MEDIA_ARTIFACT,
        artifact_id=str(new_uuid7()),
        schema_version=1,
        cache_key=digest,
        payload_hash=sha256_hex(payload),
        payload=payload,
    )
    return StepContext(
        run_id=RunId(new_uuid7()),
        item_id=ItemId(new_uuid7()),
        step_id=StepId(new_uuid7()),
        inputs={MEDIA_ARTIFACT: step_input},
        budget=BudgetSnapshot(memory_mb=16_384.0, cpu_seconds=3600.0),
    )


def _tags(title: str) -> bytes:
    payload = InitialTagsPayload(
        tags={"title": title, "artist": ["Benchmark Artist"]},
        raw=RawTagInfo(format="FLAC"),
        reader=TagReaderInfo(id="fixture", version="1"),
    )
    return payload.model_dump_json().encode("utf-8")


def _path(audio: Path) -> bytes:
    payload = PathStructurePayload(
        path=str(audio),
        parent_names=list(audio.parent.parts[-2:]),
        stem=audio.stem,
        suffix=audio.suffix,
    )
    return payload.model_dump_json().encode("utf-8")


def _probe(stems: StemsPayload) -> bytes:
    payload = AudioProbePayload(
        container="flac",
        format_name="flac",
        duration_seconds=stems.duration_seconds,
        streams=[{"codec_type": "audio", "codec_name": "flac"}],
    )
    return payload.model_dump_json().encode("utf-8")


def _write_benchmark(data: dict[str, Any]) -> None:
    destination = os.environ.get("TRACKFORGE_DEMUCS_BENCH_OUT", "").strip()
    if not destination:
        return
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.skipif(
    not AUDIO or not MODEL,
    reason="set TRACKFORGE_DEMUCS_AUDIO and TRACKFORGE_DEMUCS_MODEL",
)
async def test_demucs_slow_benchmark(tmp_path: Path) -> None:
    pytest.importorskip("torch")
    pytest.importorskip("demucs")
    audio = Path(AUDIO)
    model_path = Path(MODEL)
    assert audio.is_file(), f"audio file not found: {audio}"
    assert model_path.exists(), f"model path not found: {model_path}"

    manager = LocalModelManager(
        [
            ModelInfo(
                id="htdemucs",
                version="955717e8-8726e21a",
                kind=ModelKind.DEMUCS,
                license="MIT",
                path=str(model_path),
                heavy=True,
            )
        ],
        models_dir=tmp_path / "models",
        offline=True,
    )
    observer = DemucsObserver(
        manager,
        "htdemucs",
        segment=7.8,
        overlap=0.25,
        jobs=1,
        threads=8,
        memory_mb=1024,
        memory_available_mb=lambda: 1_000_000.0,
    )

    before_rss = _rss_mb()
    started = time.perf_counter()
    result = await asyncio.wait_for(observer.run(_context(audio)), timeout=1800)
    elapsed = time.perf_counter() - started
    peak_rss = _rss_mb()

    assert result.status in {StepResultStatus.SUCCEEDED, StepResultStatus.DEGRADED}
    stems = StemsPayload.model_validate(result.artifacts[0].payload)
    assert stems.duration_seconds > 0.0
    assert stems.ratios.vocals + stems.ratios.instrumental == pytest.approx(1.0)
    rtf = elapsed / stems.duration_seconds

    marker_item = ItemId(new_uuid7())
    stem_artifact = ArtifactPayload(
        type=str(STEMS_ARTIFACT),
        payload=canonical_json(result.artifacts[0].payload),
        artifact_id="benchmark-stems",
    )
    interpretation = derivative_detector.interpret(
        InterpreterInput(
            item_id=marker_item,
            artifacts={
                "initial.tags.v1": ArtifactPayload(
                    type="initial.tags.v1",
                    payload=_tags(f"{audio.stem} (Nightcore)"),
                    artifact_id="benchmark-tags",
                ),
                "path.structure.v1": ArtifactPayload(
                    type="path.structure.v1",
                    payload=_path(audio),
                    artifact_id="benchmark-path",
                ),
                "audio.probe.v1": ArtifactPayload(
                    type=str(PROBE_ARTIFACT),
                    payload=_probe(stems),
                    artifact_id="benchmark-probe",
                ),
                str(STEMS_ARTIFACT): stem_artifact,
            },
        )
    )
    assert interpretation.claims
    assert any(claim.field == "transformation" for claim in interpretation.claims)

    benchmark = {
        "audio": audio.name,
        "duration_seconds": round(stems.duration_seconds, 3),
        "elapsed_seconds": round(elapsed, 3),
        "rtf": round(rtf, 3),
        "rss_before_mb": round(before_rss, 1),
        "peak_rss_mb": round(peak_rss, 1),
        "vocals_ratio": round(stems.ratios.vocals, 6),
        "instrumental_ratio": round(stems.ratios.instrumental, 6),
        "vocal_energy": stems.features.vocal_energy,
        "spectral_centroid": stems.features.spectral_centroid,
        "rms": stems.features.rms,
        "status": result.status.value,
        "warnings": [warning.code for warning in result.warnings],
        "model": stems.model.model_dump(mode="json"),
    }
    _write_benchmark(benchmark)
    print("DEMUCS_BENCHMARK " + json.dumps(benchmark, sort_keys=True))

    assert rtf <= 2.0, f"Demucs RTF {rtf:.2f} exceeds the go/no-go threshold"
    assert peak_rss <= 8192.0, f"Demucs peak RSS {peak_rss:.0f} MB exceeds 8 GB"
