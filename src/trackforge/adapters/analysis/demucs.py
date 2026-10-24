"""Demucs CPU observer for ``audio.stems.v1``.

Separation is opt-in and expensive.  The observer loads ``htdemucs`` lazily,
runs a single job (``-j 1``), keeps samples in memory only, and emits just
energy ratios plus a few summary features.  Stems are never written to the
library or CAS.  Missing models, a missing runtime, or low memory produce a
visible ``skipped`` result instead of failing the observation run or an OOM.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from trackforge.application.dto.stems import (
    StemFeatures,
    StemRatios,
    StemsModelInfo,
    StemsPayload,
)
from trackforge.application.ports.execution import (
    ArtifactDraft,
    StepContext,
    StepResult,
    StepResultStatus,
    StepWarning,
)
from trackforge.application.ports.models import ModelInfo, ModelManager, ModelStatus
from trackforge.application.ports.observations import MEDIA_ARTIFACT, STEMS_ARTIFACT, media_input
from trackforge.domain.errors import DomainValidationError, ModelUnavailable
from trackforge.domain.model.capabilities import (
    CapabilityKind,
    CapabilityManifest,
    Cost,
    Determinism,
    SideEffect,
)

ModelFactory = Callable[[Path, int], Any]
Separator = Callable[[Any, Path, float, float, int], "StemSeparation"]
MemoryProbe = Callable[[], float | None]

_LOW_MEMORY_CODE = "model_oom"
_REQUIRED_SOURCES = ("drums", "bass", "other", "vocals")


@dataclass(frozen=True, slots=True)
class StemSeparation:
    """Raw in-memory separation result; audio samples are not retained."""

    energies: Mapping[str, float]
    duration_seconds: float
    rms: float
    spectral_centroid: float
    segment: float | None = None

    def __post_init__(self) -> None:
        for name in _REQUIRED_SOURCES:
            value = self.energies.get(name)
            if value is None:
                raise DomainValidationError(
                    "separation is missing a required stem",
                    context={"stem": name},
                )
            value = float(value)
            if not math.isfinite(value) or value < 0.0:
                raise DomainValidationError(
                    "stem energy must be finite and non-negative",
                    context={"stem": name, "value": str(value)},
                )
        if not math.isfinite(self.duration_seconds) or self.duration_seconds < 0.0:
            raise DomainValidationError("separation duration must be non-negative")
        if not math.isfinite(self.rms) or self.rms < 0.0:
            raise DomainValidationError("separation rms must be non-negative")
        if not math.isfinite(self.spectral_centroid) or self.spectral_centroid < 0.0:
            raise DomainValidationError("spectral centroid must be non-negative")
        if self.segment is not None and (not math.isfinite(self.segment) or self.segment <= 0.0):
            raise DomainValidationError("separation segment must be positive")


class DemucsObserver:
    """Separate one media file with Demucs and emit summary stem evidence."""

    manifest = CapabilityManifest(
        id="demucs",
        version="1",
        kind=CapabilityKind.OBSERVER,
        requires=(MEDIA_ARTIFACT,),
        provides=(STEMS_ARTIFACT,),
        cost=Cost(cpu_seconds=240.0, memory_mb=3072.0),
        determinism=Determinism.DETERMINISTIC,
        cacheable=True,
        side_effects=SideEffect.NONE,
        description="Separate vocals/instrumental with htdemucs on CPU; keep features only.",
        license="MIT (Demucs); model weights carry their own license",
        config_schema={
            "properties": {
                "stems_model": {},
                "stems_model_path": {},
                "stems_model_version": {},
                "stems_checksum": {},
                "stems_license": {},
                "stems_backend": {},
                "stems_segment": {},
                "stems_overlap": {},
                "stems_jobs": {},
                "stems_threads": {},
            }
        },
    )

    def __init__(
        self,
        model_manager: ModelManager,
        model_id: str,
        *,
        backend: str = "demucs",
        segment: float = 7.8,
        overlap: float = 0.25,
        jobs: int = 1,
        threads: int = 8,
        memory_mb: int = 3072,
        model_factory: ModelFactory | None = None,
        separator: Separator | None = None,
        memory_available_mb: MemoryProbe | None = None,
    ) -> None:
        if backend != "demucs":
            raise ValueError(f"unsupported stems backend: {backend}")
        if segment <= 0.0:
            raise ValueError("Demucs segment must be positive")
        if not 0.0 <= overlap <= 1.0:
            raise ValueError("Demucs overlap must be between 0 and 1")
        if jobs != 1:
            raise ValueError("Demucs jobs must be 1; parallel jobs multiply memory")
        if threads < 1 or threads > 16:
            raise ValueError("Demucs threads must be between 1 and 16")
        if memory_mb < 1024:
            raise ValueError("Demucs memory estimate must be at least 1024 MB")
        self._manager = model_manager
        self._model_id = model_id.strip()
        self._backend = backend
        self._segment = float(segment)
        self._overlap = float(overlap)
        self._jobs = jobs
        self._threads = threads
        self._memory_mb = memory_mb
        self._model_factory = model_factory or _load_demucs_model
        self._separator = separator or _separate_with_demucs
        self._memory_available_mb = memory_available_mb or _proc_memory_available_mb
        self._model: Any | None = None
        self._model_key: tuple[str, int] | None = None

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
            provider="demucs",
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
                    "stems_missing_media",
                    "Demucs stems require a media input artifact",
                )
            )
        try:
            media = media_input(media_step)
        except Exception as exc:
            return self._skipped(
                self._warning(
                    "stems_invalid_media",
                    f"media descriptor is invalid: {exc}",
                )
            )
        path = Path(media.cas_path)
        if not path.is_file():
            return self._skipped(
                self._warning(
                    "stems_missing_media",
                    "media bytes are missing from the content-addressed store",
                    user_action="Re-acquire the source file.",
                )
            )
        if not self._model_id:
            return self._skipped(
                self._warning(
                    "model_missing",
                    "stems are enabled but no [models.stems] model is configured",
                    user_action=(
                        "Set [models.stems] name/path/license/checksum and run "
                        "`trackforge models download demucs`."
                    ),
                )
            )

        try:
            info = self._manager.resolve(self._model_id)
        except ModelUnavailable as exc:
            return self._skipped(
                self._warning(
                    "model_missing",
                    f"Demucs model {self._model_id!r} is not configured: {exc.message}",
                    user_action=(
                        "Configure [models.stems] and run `trackforge models download demucs`."
                    ),
                )
            )
        if not info.available:
            return self._skipped(self._status_warning(info))

        if ctx.budget.memory_mb is not None and ctx.budget.memory_mb < self._memory_mb:
            return self._skipped(
                self._warning(
                    "budget_exceeded",
                    (
                        f"Demucs needs about {self._memory_mb} MB but the remaining "
                        f"budget is {ctx.budget.memory_mb:.0f} MB"
                    ),
                    user_action="Raise the memory budget or disable stems.",
                )
            )
        available = self._memory_available_mb()
        if available is not None and available < self._memory_mb:
            return self._skipped(
                self._warning(
                    _LOW_MEMORY_CODE,
                    (
                        f"Demucs needs about {self._memory_mb} MB but only "
                        f"{available:.0f} MB is available"
                    ),
                    user_action="Free memory, lower [models.stems] memory_mb, or disable stems.",
                )
            )

        try:
            model_path = self._manager.load_path(self._model_id)
        except ModelUnavailable as exc:
            return self._skipped(
                self._warning("model_missing", exc.message, user_action=exc.user_action)
            )

        ctx.progress(0.05, "loading Demucs model")
        started = time.perf_counter()
        try:
            separation = await asyncio.to_thread(self._separate, path, model_path)
        except MemoryError:
            self._release_model()
            return self._skipped(
                self._warning(
                    _LOW_MEMORY_CODE,
                    "Demucs ran out of memory; the step was skipped",
                    user_action="Free memory, use htdemucs, or disable stems.",
                )
            )
        except ImportError as exc:
            return self._skipped(
                self._warning(
                    "runtime_missing",
                    f"Demucs runtime is not installed: {exc}",
                    user_action="Install the [stems] extra: uv sync --extra stems.",
                )
            )
        except Exception as exc:
            return self._skipped(
                self._warning(
                    "demucs_failed",
                    f"Demucs separation failed: {type(exc).__name__}: {exc}",
                    user_action="Check the audio file, model checkpoint, and available memory.",
                    retryable=True,
                )
            )
        elapsed = time.perf_counter() - started
        ctx.progress(1.0, "separation complete")

        warnings: list[StepWarning] = []
        total_energy = sum(float(separation.energies[name]) for name in _REQUIRED_SOURCES)
        if total_energy <= 0.0:
            warnings.append(
                self._warning(
                    "stems_silent_audio",
                    "Demucs returned no measurable energy for any stem",
                    user_action="Check that the media file contains audible audio.",
                )
            )
        payload = build_stems_payload(
            separation,
            model=StemsModelInfo(
                id=self._model_id,
                version=info.version or "unknown",
                backend=self._backend,
                segment=separation.segment or self._segment,
                overlap=self._overlap,
                jobs=self._jobs,
            ),
            warnings=[warning.code for warning in warnings],
        )
        draft = ArtifactDraft(
            type=STEMS_ARTIFACT,
            payload=payload.model_dump(mode="json"),
            params={
                "model_id": self._model_id,
                "backend": self._backend,
                "segment": payload.model.segment,
                "overlap": self._overlap,
                "jobs": self._jobs,
            },
        )
        actual = Cost(cpu_seconds=elapsed, memory_mb=float(self._memory_mb))
        if warnings:
            return StepResult(
                artifacts=(draft,),
                warnings=tuple(warnings),
                actual_cost=actual,
                status=StepResultStatus.DEGRADED,
            )
        return StepResult(artifacts=(draft,), actual_cost=actual)

    def _status_warning(self, info: ModelInfo) -> StepWarning:
        if info.status is ModelStatus.MISSING:
            return self._warning(
                "model_missing",
                f"Demucs model {self._model_id!r} is not present locally",
                user_action=("Run `trackforge models download demucs` or set [models.stems] path."),
            )
        if info.status is ModelStatus.UNLICENSED:
            return self._warning(
                "model_unlicensed",
                f"Demucs model {self._model_id!r} has no configured license",
                user_action="Set [models.stems] license before using the model.",
            )
        if info.status is ModelStatus.CHECKSUM_MISMATCH:
            return self._warning(
                "model_checksum_mismatch",
                f"Demucs model {self._model_id!r} failed checksum verification",
                user_action="Re-download the model or fix [models.stems] checksum.",
            )
        if info.status is ModelStatus.RUNTIME_MISSING:
            return self._warning(
                "runtime_missing",
                f"Demucs runtime is unavailable: {info.metadata.get('error', 'install [stems]')}",
                user_action="Install the [stems] extra: uv sync --extra stems.",
            )
        error = info.metadata.get("error") or info.status.value
        return self._warning(
            "model_unavailable",
            f"Demucs model {self._model_id!r} is unavailable: {error}",
        )

    def _separate(self, media_path: Path, model_path: Path) -> StemSeparation:
        model = self._get_model(model_path)
        return self._separator(
            model,
            media_path,
            self._segment,
            self._overlap,
            self._jobs,
        )

    def _get_model(self, model_path: Path) -> Any:
        key = (str(model_path), self._threads)
        if self._model is not None and self._model_key == key:
            return self._model
        if self._model is not None:
            self._release_model()
        model = self._model_factory(model_path, self._threads)
        self._model = model
        self._model_key = key
        return model

    def _release_model(self) -> None:
        self._model = None
        self._model_key = None
        self._manager.release(self._model_id)


def build_stems_payload(
    separation: StemSeparation,
    *,
    model: StemsModelInfo,
    warnings: list[str] | None = None,
) -> StemsPayload:
    """Convert raw stem energies into the validated ``audio.stems.v1`` payload."""

    energies = {name: float(separation.energies[name]) for name in _REQUIRED_SOURCES}
    total = sum(energies.values())
    if total > 0.0:
        ratios = {name: round(energies[name] / total, 6) for name in _REQUIRED_SOURCES}
        instrumental = round(ratios["drums"] + ratios["bass"] + ratios["other"], 6)
    else:
        ratios = dict.fromkeys(_REQUIRED_SOURCES, 0.0)
        instrumental = 0.0
    payload = StemsPayload(
        model=model,
        duration_seconds=max(0.0, float(separation.duration_seconds)),
        ratios=StemRatios(
            vocals=ratios["vocals"],
            drums=ratios["drums"],
            bass=ratios["bass"],
            other=ratios["other"],
            instrumental=instrumental,
        ),
        features=StemFeatures(
            vocal_energy=ratios["vocals"],
            spectral_centroid=max(0.0, float(separation.spectral_centroid)),
            rms=min(1.0, max(0.0, float(separation.rms))),
        ),
        warnings=list(dict.fromkeys(warnings or ())),
    )
    return payload


def _load_demucs_model(model_path: Path, threads: int) -> Any:
    import torch  # type: ignore[import-not-found,import-untyped,unused-ignore]
    from demucs.states import (  # type: ignore[import-not-found,import-untyped,unused-ignore]
        load_model,
    )

    torch.set_num_threads(threads)
    checkpoint = _checkpoint_path(model_path)
    model = cast(Any, load_model)(str(checkpoint))
    model.eval()
    return model


def _checkpoint_path(path: Path) -> Path:
    if path.is_file():
        return path
    if path.is_dir():
        checkpoints = sorted(path.glob("*.th"))
        if len(checkpoints) == 1:
            return checkpoints[0]
        if not checkpoints:
            raise FileNotFoundError(f"no Demucs checkpoint found in {path}")
        raise FileNotFoundError(f"multiple Demucs checkpoints found in {path}")
    raise FileNotFoundError(f"Demucs model path does not exist: {path}")


def _separate_with_demucs(
    model: Any,
    media_path: Path,
    segment: float,
    overlap: float,
    jobs: int,
) -> StemSeparation:
    import torch  # type: ignore[import-not-found,import-untyped,unused-ignore]
    from demucs.apply import (  # type: ignore[import-not-found,import-untyped,unused-ignore]
        apply_model,
    )
    from demucs.audio import AudioFile  # type: ignore[import-not-found,import-untyped,unused-ignore]

    sample_rate = int(getattr(model, "samplerate", 44100))
    channels = int(getattr(model, "audio_channels", 2))
    audio = cast(Any, AudioFile)(media_path).read(
        streams=0,
        samplerate=sample_rate,
        channels=channels,
    )
    if audio.numel() == 0:
        raise ValueError("decoded audio is empty")
    reference = audio.mean(dim=0)
    scale = reference.std()
    normalized = (audio - reference.mean()) / (scale + 1e-8)
    sources = apply_model(
        model,
        normalized[None],
        device="cpu",
        shifts=0,
        split=True,
        overlap=overlap,
        progress=False,
        num_workers=jobs,
    )[0]
    sources = sources * scale + reference.mean()
    energy = sources.pow(2).mean(dim=(1, 2))
    names = tuple(str(item) for item in getattr(model, "sources", _REQUIRED_SOURCES))
    energies = {
        name: float(energy[index].item())
        for index, name in enumerate(names)
        if index < energy.numel()
    }
    for name in _REQUIRED_SOURCES:
        energies.setdefault(name, 0.0)
    rms = float(torch.sqrt(torch.mean(audio.pow(2))).item())
    actual_segment = getattr(model, "segment", None)
    return StemSeparation(
        energies=energies,
        duration_seconds=float(audio.shape[-1]) / float(sample_rate),
        rms=rms,
        spectral_centroid=_spectral_centroid(audio, sample_rate),
        segment=float(actual_segment) if actual_segment is not None else segment,
    )


def _spectral_centroid(audio: Any, sample_rate: int) -> float:
    import torch  # type: ignore[import-not-found,import-untyped,unused-ignore]

    mono = audio.mean(dim=0)
    if mono.numel() < 2 or sample_rate <= 0:
        return 0.0
    maximum = sample_rate * 60
    chunk = mono[:maximum] if mono.numel() > maximum else mono
    window = torch.hann_window(chunk.numel())
    spectrum = torch.fft.rfft(chunk * window)
    magnitude = spectrum.abs()
    total = float(magnitude.sum().item())
    if total <= 0.0:
        return 0.0
    frequencies = torch.fft.rfftfreq(chunk.numel(), d=1.0 / float(sample_rate))
    centroid = float((magnitude * frequencies).sum().item() / total)
    return max(0.0, centroid)


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


__all__ = [
    "DemucsObserver",
    "MemoryProbe",
    "ModelFactory",
    "Separator",
    "StemSeparation",
    "build_stems_payload",
]
