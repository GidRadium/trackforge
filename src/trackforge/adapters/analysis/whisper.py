"""faster-whisper CPU observer for ``asr.transcript.v1``.

The model is loaded lazily through the :class:`ModelManager`, never inside the
transcription function, and only one heavy model is resident.  Missing models,
a missing runtime, or insufficient memory produce visible ``skipped`` results
instead of failing the observation run or triggering an OOM.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from trackforge.application.dto.lyrics import (
    AsrModelInfo,
    AsrSegment,
    AsrTranscript,
    AsrWord,
    normalize_lyrics_text,
    valid_language,
)
from trackforge.application.ports.execution import (
    ArtifactDraft,
    StepContext,
    StepResult,
    StepResultStatus,
    StepWarning,
)
from trackforge.application.ports.models import ModelInfo, ModelManager, ModelStatus
from trackforge.application.ports.observations import (
    ASR_TRANSCRIPT_ARTIFACT,
    MEDIA_ARTIFACT,
    media_input,
)
from trackforge.domain.errors import ModelUnavailable
from trackforge.domain.model.capabilities import (
    CapabilityKind,
    CapabilityManifest,
    Cost,
    Determinism,
    SideEffect,
)

ModelFactory = Callable[[Path, str, int], Any]
MemoryProbe = Callable[[], float | None]

_LOW_MEMORY_CODE = "model_oom"


class WhisperObserver:
    """Transcribe media with faster-whisper on CPU and emit a structured artifact."""

    manifest = CapabilityManifest(
        id="whisper",
        version="1",
        kind=CapabilityKind.OBSERVER,
        requires=(MEDIA_ARTIFACT,),
        provides=(ASR_TRANSCRIPT_ARTIFACT,),
        cost=Cost(cpu_seconds=240.0, memory_mb=2048.0),
        determinism=Determinism.NON_DETERMINISTIC,
        cacheable=True,
        side_effects=SideEffect.NONE,
        description="Transcribe audio locally with faster-whisper (CPU int8).",
        license="MIT (faster-whisper); model weights carry their own license",
        config_schema={
            "properties": {
                "asr_model": {},
                "asr_model_path": {},
                "asr_model_version": {},
                "asr_checksum": {},
                "asr_license": {},
                "asr_backend": {},
                "asr_compute_type": {},
                "asr_threads": {},
                "asr_vad_filter": {},
                "asr_beam_size": {},
            }
        },
    )

    def __init__(
        self,
        model_manager: ModelManager,
        model_id: str,
        *,
        compute_type: str = "int8",
        threads: int = 8,
        vad_filter: bool = False,
        beam_size: int = 5,
        backend: str = "faster-whisper",
        memory_mb: int = 2048,
        model_factory: ModelFactory | None = None,
        memory_available_mb: MemoryProbe | None = None,
    ) -> None:
        if backend != "faster-whisper":
            raise ValueError(f"unsupported ASR backend: {backend}")
        if compute_type not in {"int8", "int8_float32", "float32"}:
            raise ValueError(f"unsupported ASR compute type: {compute_type}")
        if threads < 1 or threads > 16:
            raise ValueError("ASR threads must be between 1 and 16")
        if beam_size < 1 or beam_size > 10:
            raise ValueError("ASR beam_size must be between 1 and 10")
        if memory_mb < 512:
            raise ValueError("ASR memory estimate must be at least 512 MB")
        self._manager = model_manager
        self._model_id = model_id.strip()
        self._compute_type = compute_type
        self._threads = threads
        self._vad_filter = bool(vad_filter)
        self._beam_size = beam_size
        self._memory_mb = memory_mb
        self._model_factory = model_factory or _load_whisper_model
        self._memory_available_mb = memory_available_mb or _proc_memory_available_mb
        self._model: Any | None = None
        self._model_key: tuple[str, str, int] | None = None

    def _warning(
        self,
        code: str,
        message: str,
        *,
        user_action: str | None = None,
        retryable: bool = False,
    ) -> StepWarning:
        return StepWarning(
            code=code,
            message=message,
            capability_id=self.manifest.id,
            provider="faster-whisper",
            retryable=retryable,
            user_action=user_action,
        )

    def _skipped(self, warning: StepWarning) -> StepResult:
        return StepResult.skipped(warnings=(warning,))

    async def run(self, ctx: StepContext) -> StepResult:
        media_step = ctx.inputs.get(MEDIA_ARTIFACT)
        if media_step is None:
            return self._skipped(
                self._warning(
                    "asr_missing_media",
                    "Whisper ASR requires a media input artifact",
                )
            )
        try:
            media = media_input(media_step)
        except Exception as exc:
            return self._skipped(
                self._warning(
                    "asr_invalid_media",
                    f"media descriptor is invalid: {exc}",
                )
            )
        path = Path(media.cas_path)
        if not path.is_file():
            return self._skipped(
                self._warning(
                    "asr_missing_media",
                    "media bytes are missing from the content-addressed store",
                    user_action="Re-acquire the source file.",
                )
            )
        if not self._model_id:
            return self._skipped(
                self._warning(
                    "model_missing",
                    "ASR is enabled but no [models.asr] model is configured",
                    user_action=(
                        "Set [models.asr] name/path/license and run "
                        "`trackforge models download asr`."
                    ),
                )
            )

        try:
            info = self._manager.resolve(self._model_id)
        except ModelUnavailable as exc:
            return self._skipped(
                self._warning(
                    "model_missing",
                    f"ASR model {self._model_id!r} is not configured: {exc.message}",
                    user_action="Configure [models.asr] and run `trackforge models download asr`.",
                )
            )
        if not info.available:
            return self._skipped(self._status_warning(info))

        if ctx.budget.memory_mb is not None and ctx.budget.memory_mb < self._memory_mb:
            return self._skipped(
                self._warning(
                    "budget_exceeded",
                    (
                        f"ASR needs about {self._memory_mb} MB but the remaining "
                        f"budget is {ctx.budget.memory_mb:.0f} MB"
                    ),
                    user_action="Raise the memory budget or disable ASR.",
                )
            )
        available = self._memory_available_mb()
        if available is not None and available < self._memory_mb:
            return self._skipped(
                self._warning(
                    _LOW_MEMORY_CODE,
                    (
                        f"ASR needs about {self._memory_mb} MB but only "
                        f"{available:.0f} MB is available"
                    ),
                    user_action="Free memory, lower [models.asr] memory_mb, or disable ASR.",
                )
            )

        try:
            model_path = self._manager.load_path(self._model_id)
        except ModelUnavailable as exc:
            return self._skipped(
                self._warning(
                    "model_missing",
                    exc.message,
                    user_action=exc.user_action,
                )
            )

        ctx.note("loading Whisper model")
        started = time.perf_counter()
        loop = asyncio.get_running_loop()

        def report_progress(fraction: float, message: str | None = None) -> None:
            loop.call_soon_threadsafe(ctx.progress, fraction, message)

        try:
            transcript = await asyncio.to_thread(
                self._transcribe,
                path,
                model_path,
                info,
                report_progress,
            )
        except MemoryError:
            self._release_model()
            return self._skipped(
                self._warning(
                    _LOW_MEMORY_CODE,
                    "Whisper ran out of memory; the step was skipped",
                    user_action="Free memory, use a smaller model, or disable ASR.",
                )
            )
        except ImportError as exc:
            return self._skipped(
                self._warning(
                    "runtime_missing",
                    f"faster-whisper is not installed: {exc}",
                    user_action="Install the [asr] extra: uv sync --extra asr.",
                )
            )
        except Exception as exc:
            return self._skipped(
                self._warning(
                    "asr_failed",
                    f"Whisper transcription failed: {type(exc).__name__}: {exc}",
                    user_action="Check the audio file, model version, and available memory.",
                    retryable=True,
                )
            )
        elapsed = time.perf_counter() - started
        ctx.progress(1.0, "transcription complete")
        realtime = (
            transcript.duration_seconds / elapsed
            if elapsed > 0.0 and transcript.duration_seconds > 0.0
            else 0.0
        )
        summary = (
            f"{transcript.language or 'und'}, {len(transcript.segments)} segment(s), "
            f"{realtime:.1f}× realtime"
        )
        details = {
            "language": transcript.language,
            "segments": len(transcript.segments),
            "duration_seconds": transcript.duration_seconds,
            "realtime_factor": round(realtime, 3),
        }

        warnings: list[StepWarning] = []
        if transcript.empty:
            warnings.append(
                self._warning(
                    "empty_transcript",
                    "Whisper returned no speech segments",
                    user_action="Check the language/model or treat the track as instrumental.",
                )
            )
        if transcript.language is None:
            warnings.append(
                self._warning(
                    "asr_language_unknown",
                    "Whisper did not produce a valid ISO 639 language code",
                )
            )
        draft = ArtifactDraft(
            type=ASR_TRANSCRIPT_ARTIFACT,
            payload=transcript.model_dump(mode="json"),
            params={
                "model_id": self._model_id,
                "compute_type": self._compute_type,
                "threads": self._threads,
            },
        )
        actual = Cost(cpu_seconds=elapsed, memory_mb=float(self._memory_mb))
        if warnings:
            return StepResult(
                artifacts=(draft,),
                warnings=tuple(warnings),
                actual_cost=actual,
                status=StepResultStatus.DEGRADED,
                summary=summary,
                details=details,
            )
        return StepResult(
            artifacts=(draft,),
            actual_cost=actual,
            summary=summary,
            details=details,
        )

    def _status_warning(self, info: ModelInfo) -> StepWarning:
        if info.status is ModelStatus.MISSING:
            return self._warning(
                "model_missing",
                f"ASR model {self._model_id!r} is not present locally",
                user_action="Run `trackforge models download asr` or set [models.asr] path.",
            )
        if info.status is ModelStatus.UNLICENSED:
            return self._warning(
                "model_unlicensed",
                f"ASR model {self._model_id!r} has no configured license",
                user_action="Set [models.asr] license before using the model.",
            )
        if info.status is ModelStatus.CHECKSUM_MISMATCH:
            return self._warning(
                "model_checksum_mismatch",
                f"ASR model {self._model_id!r} failed checksum verification",
                user_action="Re-download the model or fix [models.asr] checksum.",
            )
        if info.status is ModelStatus.RUNTIME_MISSING:
            return self._warning(
                "runtime_missing",
                f"ASR runtime is unavailable: {info.metadata.get('error', 'install [asr]')}",
                user_action="Install the [asr] extra: uv sync --extra asr.",
            )
        error = info.metadata.get("error") or info.status.value
        return self._warning(
            "model_unavailable",
            f"ASR model {self._model_id!r} is unavailable: {error}",
        )

    def _get_model(self, model_path: Path) -> Any:
        key = (str(model_path), self._compute_type, self._threads)
        if self._model is not None and self._model_key == key:
            return self._model
        if self._model is not None:
            self._release_model()
        model = self._model_factory(model_path, self._compute_type, self._threads)
        self._model = model
        self._model_key = key
        return model

    def _release_model(self) -> None:
        self._model = None
        self._model_key = None
        self._manager.release(self._model_id)

    def _transcribe(
        self,
        media_path: Path,
        model_path: Path,
        info: ModelInfo,
        progress: Callable[[float, str | None], None] | None = None,
    ) -> AsrTranscript:
        model = self._get_model(model_path)
        segments, metadata = model.transcribe(
            str(media_path),
            language=None,
            beam_size=self._beam_size,
            vad_filter=self._vad_filter,
            word_timestamps=True,
            condition_on_previous_text=False,
            temperature=0.0,
        )
        decoded_segments: list[AsrSegment] = []
        logprobs: list[float] = []
        no_speech: list[float] = []
        last_end = 0.0
        raw_duration = getattr(metadata, "duration", None)
        try:
            total_duration = float(raw_duration) if raw_duration is not None else None
        except (TypeError, ValueError):
            total_duration = None
        if total_duration is not None and total_duration <= 0.0:
            total_duration = None
        last_report = 0.0
        for segment in segments:
            text = normalize_lyrics_text(str(getattr(segment, "text", "") or ""))
            start = max(0.0, float(getattr(segment, "start", 0.0) or 0.0))
            end = max(start, float(getattr(segment, "end", start) or start))
            last_end = max(last_end, end)
            if progress is not None and total_duration:
                now = time.monotonic()
                if now - last_report >= 0.25:
                    last_report = now
                    fraction = max(0.0, min(1.0, end / total_duration))
                    progress(
                        fraction,
                        f"{end:.0f}s / {total_duration:.0f}s",
                    )
            logprob = getattr(segment, "avg_logprob", None)
            if isinstance(logprob, (int, float)):
                logprobs.append(float(logprob))
            speech = getattr(segment, "no_speech_prob", None)
            if isinstance(speech, (int, float)):
                no_speech.append(float(speech))
            words: list[AsrWord] = []
            for raw_word in getattr(segment, "words", None) or ():
                token = normalize_lyrics_text(str(getattr(raw_word, "word", "") or ""))
                if not token:
                    continue
                word_start = max(start, float(getattr(raw_word, "start", start) or start))
                word_end = max(
                    word_start,
                    float(getattr(raw_word, "end", word_start) or word_start),
                )
                probability = getattr(raw_word, "probability", None)
                if isinstance(probability, (int, float)):
                    probability = max(0.0, min(1.0, float(probability)))
                else:
                    probability = None
                words.append(
                    AsrWord(
                        word=token,
                        start=word_start,
                        end=word_end,
                        probability=probability,
                    )
                )
            if not text and not words:
                continue
            decoded_segments.append(AsrSegment(start=start, end=end, text=text, words=words))

        duration_seconds = total_duration if total_duration is not None else last_end
        language = valid_language(getattr(metadata, "language", None))
        return AsrTranscript(
            language=language,
            segments=decoded_segments,
            avg_logprob=(sum(logprobs) / len(logprobs) if logprobs else None),
            no_speech_prob=(
                max(0.0, min(1.0, sum(no_speech) / len(no_speech))) if no_speech else None
            ),
            model=AsrModelInfo(
                id=self._model_id,
                version=info.version or "unknown",
                compute_type=self._compute_type,
                threads=self._threads,
            ),
            duration_seconds=max(0.0, duration_seconds),
        )


def _load_whisper_model(model_path: Path, compute_type: str, threads: int) -> Any:
    from faster_whisper import WhisperModel  # type: ignore[import-not-found,import-untyped,unused-ignore]

    return WhisperModel(
        str(model_path),
        device="cpu",
        compute_type=compute_type,
        cpu_threads=threads,
        num_workers=1,
    )


def _proc_memory_available_mb() -> float | None:
    try:
        with Path("/proc/meminfo").open(encoding="utf-8") as file:
            for line in file:
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) / 1024.0
    except (OSError, ValueError):
        return None
    return None


__all__ = ["ModelFactory", "WhisperObserver"]
