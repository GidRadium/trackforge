"""WhisperObserver contract tests with a fake model: no faster-whisper needed."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from trackforge.adapters.analysis.whisper import WhisperObserver
from trackforge.adapters.models.local import LocalModelManager
from trackforge.application.dto.lyrics import AsrTranscript
from trackforge.application.dto.observations import MediaInput
from trackforge.application.ports.execution import (
    ArtifactDraft,
    BudgetSnapshot,
    StepContext,
    StepInput,
    StepResultStatus,
)
from trackforge.application.ports.models import ModelInfo, ModelKind
from trackforge.application.ports.observations import (
    ASR_TRANSCRIPT_ARTIFACT,
    MEDIA_ARTIFACT,
)
from trackforge.application.services.canonical_json import canonical_json, sha256_hex
from trackforge.domain.model.ids import ItemId, RunId, StepId, new_uuid7

pytestmark = pytest.mark.asyncio


class _FakeModel:
    def __init__(self, segments: list[Any], language: str = "en") -> None:
        self.segments = segments
        self.language = language
        self.calls: list[dict[str, Any]] = []

    def transcribe(self, path: str, **kwargs: Any) -> tuple[list[Any], Any]:
        self.calls.append({"path": path, **kwargs})
        return self.segments, SimpleNamespace(
            language=self.language,
            language_probability=0.98,
            duration=42.0,
        )


def _segment(
    text: str,
    start: float,
    end: float,
    *,
    words: bool = True,
    no_speech: float = 0.02,
) -> Any:
    token_models = []
    if words:
        tokens = text.split()
        for index, token in enumerate(tokens):
            token_models.append(
                SimpleNamespace(
                    word=f" {token}",
                    start=start + index * 0.2,
                    end=start + index * 0.2 + 0.15,
                    probability=0.91,
                )
            )
    return SimpleNamespace(
        text=f" {text}",
        start=start,
        end=end,
        words=token_models,
        avg_logprob=-0.18,
        no_speech_prob=no_speech,
    )


def _manager(tmp_path: Path, *, runtime: bool = True) -> LocalModelManager:
    model_dir = tmp_path / "models" / "asr" / "Systran--faster-whisper-small"
    model_dir.mkdir(parents=True)
    (model_dir / "model.bin").write_bytes(b"fake-weights")
    spec = ModelInfo(
        id="Systran/faster-whisper-small",
        version="test-rev",
        kind=ModelKind.ASR,
        license="MIT",
        path=str(model_dir),
        heavy=True,
    )
    return LocalModelManager(
        [spec],
        models_dir=tmp_path / "models",
        runtime_available=lambda _name: runtime,
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
    fake: _FakeModel,
    *,
    memory_available: float | None = 8192.0,
) -> WhisperObserver:
    return WhisperObserver(
        manager,
        "Systran/faster-whisper-small",
        vad_filter=True,
        model_factory=lambda _path, _compute, _threads: fake,
        memory_available_mb=lambda: memory_available,
    )


async def test_whisper_observer_emits_valid_transcript(tmp_path: Path) -> None:
    fake = _FakeModel([_segment("hello world", 1.0, 2.0)])
    observer = _observer(_manager(tmp_path), fake)
    assert observer._model is None

    result = await observer.run(_context(tmp_path))

    assert result.status is StepResultStatus.SUCCEEDED
    assert len(result.artifacts) == 1
    draft: ArtifactDraft = result.artifacts[0]
    assert draft.type == ASR_TRANSCRIPT_ARTIFACT
    transcript = AsrTranscript.model_validate(draft.payload)
    assert transcript.language == "en"
    assert transcript.duration_seconds == 42.0
    assert [segment.text for segment in transcript.segments] == ["hello world"]
    assert transcript.segments[0].words[0].word == "hello"
    assert transcript.model.id == "Systran/faster-whisper-small"
    assert transcript.avg_logprob == pytest.approx(-0.18)
    assert fake.calls[0]["vad_filter"] is True
    assert fake.calls[0]["word_timestamps"] is True


async def test_whisper_model_is_lazy_until_run(tmp_path: Path) -> None:
    calls: list[Path] = []

    def factory(path: Path, _compute: str, _threads: int) -> _FakeModel:
        calls.append(path)
        return _FakeModel([_segment("lazy", 0.5, 1.0)])

    observer = WhisperObserver(
        _manager(tmp_path),
        "Systran/faster-whisper-small",
        model_factory=factory,
        memory_available_mb=lambda: 8192.0,
    )
    assert calls == []

    await observer.run(_context(tmp_path))

    assert len(calls) == 1


async def test_missing_model_is_skipped_with_model_missing(tmp_path: Path) -> None:
    manager = LocalModelManager([], runtime_available=lambda _name: True)
    observer = WhisperObserver(
        manager,
        "absent/model",
        model_factory=lambda *_args: _FakeModel([]),
        memory_available_mb=lambda: 8192.0,
    )

    result = await observer.run(_context(tmp_path))

    assert result.status is StepResultStatus.SKIPPED
    assert result.warnings[0].code == "model_missing"


async def test_unconfigured_model_is_skipped_before_resolution(tmp_path: Path) -> None:
    manager = LocalModelManager([], runtime_available=lambda _name: True)
    observer = WhisperObserver(
        manager,
        "",
        model_factory=lambda *_args: _FakeModel([]),
        memory_available_mb=lambda: 8192.0,
    )

    result = await observer.run(_context(tmp_path))

    assert result.status is StepResultStatus.SKIPPED
    assert result.warnings[0].code == "model_missing"


async def test_low_memory_is_skipped_without_loading_model(tmp_path: Path) -> None:
    calls: list[Path] = []

    def factory(path: Path, _compute: str, _threads: int) -> _FakeModel:
        calls.append(path)
        return _FakeModel([])

    observer = WhisperObserver(
        _manager(tmp_path),
        "Systran/faster-whisper-small",
        model_factory=factory,
        memory_available_mb=lambda: 200.0,
    )

    result = await observer.run(_context(tmp_path))

    assert result.status is StepResultStatus.SKIPPED
    assert result.warnings[0].code == "model_oom"
    assert calls == []


async def test_runtime_missing_is_visible(tmp_path: Path) -> None:
    manager = LocalModelManager(
        [
            ModelInfo(
                id="Systran/faster-whisper-small",
                kind=ModelKind.ASR,
                license="MIT",
                path=str(tmp_path / "models" / "asr" / "Systran--faster-whisper-small"),
            )
        ],
        models_dir=tmp_path / "models",
        runtime_available=lambda _name: False,
    )
    target = tmp_path / "models" / "asr" / "Systran--faster-whisper-small"
    target.mkdir(parents=True)
    (target / "model.bin").write_bytes(b"weights")
    observer = WhisperObserver(
        manager,
        "Systran/faster-whisper-small",
        model_factory=lambda *_args: _FakeModel([]),
        memory_available_mb=lambda: 8192.0,
    )

    result = await observer.run(_context(tmp_path))

    assert result.status is StepResultStatus.SKIPPED
    assert result.warnings[0].code == "runtime_missing"


async def test_empty_transcript_is_degraded_but_valid(tmp_path: Path) -> None:
    fake = _FakeModel([])
    observer = _observer(_manager(tmp_path), fake)

    result = await observer.run(_context(tmp_path))

    assert result.status is StepResultStatus.DEGRADED
    assert "empty_transcript" in {warning.code for warning in result.warnings}
    transcript = AsrTranscript.model_validate(result.artifacts[0].payload)
    assert transcript.segments == []
    assert transcript.empty is True


async def test_invalid_language_becomes_null_evidence(tmp_path: Path) -> None:
    fake = _FakeModel([_segment("hello", 0.5, 1.0)], language="not-a-language")
    observer = _observer(_manager(tmp_path), fake)

    result = await observer.run(_context(tmp_path))

    transcript = AsrTranscript.model_validate(result.artifacts[0].payload)
    assert transcript.language is None
    assert "asr_language_unknown" in {warning.code for warning in result.warnings}
