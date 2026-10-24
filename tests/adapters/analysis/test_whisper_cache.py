"""Whisper cache-key contract: model identity invalidates, memory does not."""

from __future__ import annotations

import pytest

from trackforge.adapters.analysis.whisper import WhisperObserver
from trackforge.application.ports.execution import StepInput
from trackforge.application.ports.observations import MEDIA_ARTIFACT
from trackforge.pipeline.cache import cache_key_for_step

pytestmark = pytest.mark.unit


def test_whisper_cache_key_tracks_model_configuration() -> None:
    inputs = {
        MEDIA_ARTIFACT: StepInput(
            type=MEDIA_ARTIFACT,
            artifact_id="artifact",
            schema_version=1,
            cache_key="input",
            payload_hash="a" * 64,
            payload=b"{}",
        )
    }
    first = cache_key_for_step(
        inputs,
        WhisperObserver.manifest,
        config={"asr_model": "model-a", "asr_compute_type": "int8"},
    )
    second = cache_key_for_step(
        inputs,
        WhisperObserver.manifest,
        config={"asr_model": "model-b", "asr_compute_type": "int8"},
    )

    assert first != second


def test_whisper_cache_key_ignores_memory_budget() -> None:
    inputs = {
        MEDIA_ARTIFACT: StepInput(
            type=MEDIA_ARTIFACT,
            artifact_id="artifact",
            schema_version=1,
            cache_key="input",
            payload_hash="b" * 64,
            payload=b"{}",
        )
    }
    first = cache_key_for_step(
        inputs,
        WhisperObserver.manifest,
        config={"asr_model": "model-a", "asr_memory_mb": 2048},
    )
    second = cache_key_for_step(
        inputs,
        WhisperObserver.manifest,
        config={"asr_model": "model-a", "asr_memory_mb": 4096},
    )

    assert first == second
