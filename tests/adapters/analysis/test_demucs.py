"""Demucs observer contract tests with a fake separator: no torch needed."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from trackforge.adapters.analysis.demucs import (
    DemucsObserver,
    StemSeparation,
    build_stems_payload,
)
from trackforge.adapters.models.local import LocalModelManager
from trackforge.application.dto.observations import MediaInput
from trackforge.application.dto.stems import StemsModelInfo, StemsPayload
from trackforge.application.ports.execution import (
    ArtifactDraft,
    BudgetSnapshot,
    StepContext,
    StepInput,
    StepResultStatus,
)
from trackforge.application.ports.models import ModelInfo, ModelKind
from trackforge.application.ports.observations import MEDIA_ARTIFACT, STEMS_ARTIFACT
from trackforge.application.services.canonical_json import canonical_json, sha256_hex
from trackforge.domain.model.ids import ItemId, RunId, StepId, new_uuid7

pytestmark = pytest.mark.unit


def _separation(**overrides: Any) -> StemSeparation:
    data: dict[str, Any] = {
        "energies": {
            "drums": 0.16,
            "bass": 0.10,
            "other": 0.18,
            "vocals": 0.20,
        },
        "duration_seconds": 180.0,
        "rms": 0.21,
        "spectral_centroid": 2450.0,
        "segment": 7.8,
    }
    data.update(overrides)
    return StemSeparation(**data)


def _manager(tmp_path: Path, *, runtime: bool = True) -> LocalModelManager:
    checkpoint = tmp_path / "models" / "stems" / "htdemucs" / "955717e8-8726e21a.th"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_bytes(b"fake-demucs-weights")
    spec = ModelInfo(
        id="htdemucs",
        version="955717e8-8726e21a",
        kind=ModelKind.DEMUCS,
        license="MIT",
        path=str(checkpoint),
        heavy=True,
    )
    return LocalModelManager(
        [spec],
        models_dir=tmp_path / "models",
        runtime_available=lambda name: runtime and name in {"torch", "demucs"},
    )


def _context(tmp_path: Path) -> StepContext:
    media_path = tmp_path / "audio.mp3"
    media_path.write_bytes(b"audio")
    descriptor = MediaInput(
        media_id=str(new_uuid7()),
        content_hash=hashlib.sha256(b"audio").hexdigest(),
        size_bytes=5,
        cas_path=str(media_path),
        container="mp3",
    )
    payload = canonical_json(descriptor.model_dump(mode="json"))
    step_input = StepInput(
        type=MEDIA_ARTIFACT,
        artifact_id=str(new_uuid7()),
        schema_version=1,
        cache_key=descriptor.content_hash,
        payload_hash=sha256_hex(payload),
        payload=payload,
    )
    return StepContext(
        run_id=RunId(new_uuid7()),
        item_id=ItemId(new_uuid7()),
        step_id=StepId(new_uuid7()),
        inputs={MEDIA_ARTIFACT: step_input},
        budget=BudgetSnapshot(memory_mb=8192.0),
    )


def _observer(
    manager: LocalModelManager,
    separation: StemSeparation | None = None,
    *,
    memory_available: float | None = 8192.0,
    factory: Any | None = None,
) -> DemucsObserver:
    result = separation or _separation()
    return DemucsObserver(
        manager,
        "htdemucs",
        segment=7.8,
        overlap=0.25,
        jobs=1,
        threads=4,
        memory_mb=3072,
        model_factory=factory or (lambda _path, _threads: object()),
        separator=lambda *_args: result,
        memory_available_mb=lambda: memory_available,
    )


@pytest.mark.asyncio
async def test_demucs_observer_emits_valid_stem_payload(tmp_path: Path) -> None:
    observer = _observer(_manager(tmp_path))
    assert observer._model is None

    result = await observer.run(_context(tmp_path))

    assert result.status is StepResultStatus.SUCCEEDED
    assert len(result.artifacts) == 1
    draft: ArtifactDraft = result.artifacts[0]
    assert draft.type == STEMS_ARTIFACT
    payload = StemsPayload.model_validate(draft.payload)
    assert payload.model.id == "htdemucs"
    assert payload.model.backend == "demucs"
    assert payload.model.segment == pytest.approx(7.8)
    assert payload.model.overlap == pytest.approx(0.25)
    assert payload.model.jobs == 1
    assert payload.duration_seconds == pytest.approx(180.0)
    assert payload.ratios.vocals == pytest.approx(0.20 / 0.64, abs=1e-6)
    assert payload.ratios.instrumental == pytest.approx(
        (0.16 + 0.10 + 0.18) / 0.64,
        abs=1e-6,
    )
    assert payload.ratios.vocals + payload.ratios.instrumental == pytest.approx(1.0)
    assert payload.features.vocal_energy == pytest.approx(payload.ratios.vocals)
    assert payload.features.spectral_centroid == pytest.approx(2450.0)
    assert payload.warnings == []
    assert not list(tmp_path.glob("**/*.wav"))


@pytest.mark.asyncio
async def test_demucs_model_is_lazy_until_run(tmp_path: Path) -> None:
    calls: list[Path] = []

    def factory(path: Path, _threads: int) -> object:
        calls.append(path)
        return object()

    observer = _observer(_manager(tmp_path), factory=factory)
    assert calls == []
    await observer.run(_context(tmp_path))
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_missing_model_is_skipped_with_model_missing(tmp_path: Path) -> None:
    manager = LocalModelManager([], runtime_available=lambda _name: True)
    observer = DemucsObserver(
        manager,
        "htdemucs",
        model_factory=lambda *_args: pytest.fail("model must not load"),
        separator=lambda *_args: _separation(),
    )

    result = await observer.run(_context(tmp_path))

    assert result.status is StepResultStatus.SKIPPED
    assert result.warnings[0].code == "model_missing"


@pytest.mark.asyncio
async def test_low_memory_is_skipped_without_loading_model(tmp_path: Path) -> None:
    calls: list[Path] = []

    def factory(path: Path, _threads: int) -> object:
        calls.append(path)
        return object()

    observer = _observer(_manager(tmp_path), memory_available=200.0, factory=factory)

    result = await observer.run(_context(tmp_path))

    assert result.status is StepResultStatus.SKIPPED
    assert result.warnings[0].code == "model_oom"
    assert calls == []


@pytest.mark.asyncio
async def test_runtime_missing_is_visible(tmp_path: Path) -> None:
    observer = _observer(_manager(tmp_path, runtime=False))

    result = await observer.run(_context(tmp_path))

    assert result.status is StepResultStatus.SKIPPED
    assert result.warnings[0].code == "runtime_missing"


@pytest.mark.asyncio
async def test_memory_error_releases_model(tmp_path: Path) -> None:
    manager = _manager(tmp_path)

    def exploding(*_args: Any) -> StemSeparation:
        raise MemoryError

    observer = DemucsObserver(
        manager,
        "htdemucs",
        model_factory=lambda _path, _threads: object(),
        separator=exploding,
        memory_available_mb=lambda: 8192.0,
    )

    result = await observer.run(_context(tmp_path))

    assert result.status is StepResultStatus.SKIPPED
    assert result.warnings[0].code == "model_oom"
    assert manager.active_heavy is None


@pytest.mark.asyncio
async def test_silent_audio_is_degraded_but_valid(tmp_path: Path) -> None:
    observer = _observer(
        _manager(tmp_path),
        _separation(
            energies={"drums": 0.0, "bass": 0.0, "other": 0.0, "vocals": 0.0},
            rms=0.0,
        ),
    )

    result = await observer.run(_context(tmp_path))

    assert result.status is StepResultStatus.DEGRADED
    assert "stems_silent_audio" in {warning.code for warning in result.warnings}
    payload = StemsPayload.model_validate(result.artifacts[0].payload)
    assert payload.ratios.vocals == 0.0
    assert payload.ratios.instrumental == 0.0


def test_parallel_jobs_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="jobs"):
        DemucsObserver(_manager(tmp_path), "htdemucs", jobs=2)


def test_separation_rejects_missing_stem() -> None:
    with pytest.raises(Exception, match="required stem"):
        StemSeparation(
            energies={"drums": 0.2, "bass": 0.2, "other": 0.2},
            duration_seconds=1.0,
            rms=0.1,
            spectral_centroid=100.0,
        )


def test_build_stems_payload_accepts_silence() -> None:
    payload = build_stems_payload(
        StemSeparation(
            energies={"drums": 0.0, "bass": 0.0, "other": 0.0, "vocals": 0.0},
            duration_seconds=1.0,
            rms=0.0,
            spectral_centroid=0.0,
        ),
        model=StemsModelInfo(id="htdemucs"),
    )

    assert payload.ratios.vocals == 0.0
    assert payload.ratios.instrumental == 0.0


def test_stem_ratios_reject_non_normalized_values() -> None:
    from pydantic import ValidationError

    from trackforge.application.dto.stems import StemRatios

    with pytest.raises(ValidationError, match="sum to 1"):
        StemRatios(vocals=0.2, drums=0.2, bass=0.2, other=0.2, instrumental=0.6)

    with pytest.raises(ValidationError, match="instrumental"):
        StemRatios(vocals=0.5, drums=0.2, bass=0.2, other=0.1, instrumental=0.4)


def test_stem_payload_rejects_negative_duration() -> None:
    from pydantic import ValidationError

    from trackforge.application.dto.stems import StemsPayload

    with pytest.raises(ValidationError):
        StemsPayload(
            model=StemsModelInfo(id="htdemucs"),
            duration_seconds=-1.0,
            ratios={
                "vocals": 0.5,
                "drums": 0.2,
                "bass": 0.2,
                "other": 0.1,
                "instrumental": 0.5,
            },
        )
