"""Local model registry and lazy model-manager ports.

Model weights are data, never package dependencies.  The manager validates
licenses/checksums, keeps at most one heavy model resident, and downloads only
when an explicit ``download`` call is made.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model._validation import (
    ensure_int,
    json_mapping,
    require_text,
    utc_now,
)


class ModelStatus(StrEnum):
    """Observable availability of one model artifact."""

    AVAILABLE = "available"
    MISSING = "missing"
    CHECKSUM_MISMATCH = "checksum_mismatch"
    UNLICENSED = "unlicensed"
    RUNTIME_MISSING = "runtime_missing"
    ERROR = "error"


class ModelKind(StrEnum):
    RANKER = "ranker"
    ASR = "asr"
    DEMUCS = "demucs"
    LOCAL_LLM = "local_llm"


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """Registry-visible description of one configured model."""

    id: str
    version: str = ""
    kind: ModelKind = ModelKind.RANKER
    license: str = ""
    status: ModelStatus = ModelStatus.MISSING
    path: str | None = None
    checksum: str | None = None
    size_bytes: int | None = None
    source: str | None = None
    heavy: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", require_text(self.id, "id"))
        object.__setattr__(self, "version", _text_or_empty(self.version, "version"))
        object.__setattr__(self, "kind", ModelKind(str(self.kind)))
        object.__setattr__(self, "status", ModelStatus(str(self.status)))
        object.__setattr__(self, "license", _text_or_empty(self.license, "license"))
        object.__setattr__(self, "path", _optional_nonempty(self.path, "path"))
        object.__setattr__(self, "checksum", _optional_nonempty(self.checksum, "checksum"))
        object.__setattr__(self, "source", _optional_nonempty(self.source, "source"))
        if self.size_bytes is not None:
            object.__setattr__(
                self,
                "size_bytes",
                ensure_int(self.size_bytes, "size_bytes", minimum=0),
            )
        object.__setattr__(self, "heavy", bool(self.heavy))
        object.__setattr__(self, "metadata", json_mapping(self.metadata, "metadata"))
        if not isinstance(self.updated_at, datetime):
            raise DomainValidationError("updated_at must be a datetime")

    @property
    def available(self) -> bool:
        return self.status is ModelStatus.AVAILABLE

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "kind": self.kind.value,
            "license": self.license,
            "status": self.status.value,
            "path": self.path,
            "checksum": self.checksum,
            "size_bytes": self.size_bytes,
            "source": self.source,
            "heavy": self.heavy,
            "metadata": dict(self.metadata),
        }


@runtime_checkable
class ModelManager(Protocol):
    """Lazy, offline-first access to configured local model artifacts."""

    def list_models(self) -> tuple[ModelInfo, ...]: ...

    def resolve(self, model_id: str) -> ModelInfo: ...

    def load_path(self, model_id: str) -> Path: ...

    def release(self, model_id: str | None = None) -> None: ...

    def acquire_heavy(self, model_id: str) -> None: ...

    def download(self, model_id: str, *, force: bool = False) -> ModelInfo: ...


@runtime_checkable
class ModelRegistry(Protocol):
    """Persist model availability without owning weight bytes."""

    async def list_models(self) -> list[ModelInfo]: ...

    async def get_model(self, model_id: str) -> ModelInfo | None: ...

    async def upsert_model(self, model: ModelInfo) -> ModelInfo: ...


def _text_or_empty(value: object, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise DomainValidationError(
            f"{field} must be a string",
            context={"field": field, "actual_type": type(value).__name__},
        )
    return value if value.strip() else ""


def _optional_nonempty(value: object, field: str) -> str | None:
    if value is None:
        return None
    text = _text_or_empty(value, field)
    return text or None


def model_specs_from_config(config: object) -> tuple[ModelInfo, ...]:
    """Project configured model fields into registry-visible specs.

    Kept in the port module so both the CLI and the manager use exactly the
    same projection and never infer a license from a model name.
    """

    specs: list[ModelInfo] = []
    ranker_model = str(getattr(config, "ranker_model", "") or "").strip()
    ranker_path = str(getattr(config, "ranker_model_path", "") or "").strip()
    if ranker_model or ranker_path:
        specs.append(
            ModelInfo(
                id=ranker_model or Path(ranker_path).stem,
                version=str(getattr(config, "ranker_model_version", "") or ""),
                kind=ModelKind.RANKER,
                license=str(getattr(config, "ranker_license", "") or "").strip(),
                status=ModelStatus.MISSING,
                path=ranker_path or None,
                checksum=str(getattr(config, "ranker_checksum", "") or "").strip() or None,
                size_bytes=_optional_int(getattr(config, "ranker_size_bytes", None)),
                source="config",
                heavy=bool(getattr(config, "ranker_heavy", True)),
                metadata={"strategy": str(getattr(config, "ranker_strategy", "lexical"))},
            )
        )

    stems_model = str(getattr(config, "stems_model", "") or "").strip()
    stems_path = str(getattr(config, "stems_model_path", "") or "").strip()
    if stems_model or stems_path:
        known = _KNOWN_DEMUCS.get(stems_model, {})
        version = str(getattr(config, "stems_model_version", "") or "").strip() or str(
            known.get("version", "")
        )
        specs.append(
            ModelInfo(
                id=stems_model or Path(stems_path).stem,
                version=version,
                kind=ModelKind.DEMUCS,
                license=str(getattr(config, "stems_license", "") or "").strip(),
                status=ModelStatus.MISSING,
                path=stems_path or None,
                checksum=(
                    str(getattr(config, "stems_checksum", "") or "").strip()
                    or str(known.get("checksum", ""))
                )
                or None,
                size_bytes=_optional_int(getattr(config, "stems_size_bytes", None))
                or _optional_int(known.get("size_bytes")),
                source="config",
                heavy=bool(getattr(config, "stems_heavy", True)),
                metadata={
                    "backend": str(getattr(config, "stems_backend", "demucs")),
                    "segment": float(getattr(config, "stems_segment", 7.8)),
                    "overlap": float(getattr(config, "stems_overlap", 0.25)),
                    "jobs": _optional_int(getattr(config, "stems_jobs", 1)),
                    "threads": _optional_int(getattr(config, "stems_threads", None)),
                    "url": (
                        str(getattr(config, "stems_model_url", "") or "").strip()
                        or str(known.get("url", ""))
                    ),
                    "file_name": (
                        str(getattr(config, "stems_model_file", "") or "").strip()
                        or str(known.get("file_name", ""))
                    ),
                },
            )
        )

    asr_model = str(getattr(config, "asr_model", "") or "").strip()
    asr_path = str(getattr(config, "asr_model_path", "") or "").strip()
    if asr_model or asr_path:
        specs.append(
            ModelInfo(
                id=asr_model or Path(asr_path).stem,
                version=str(getattr(config, "asr_model_version", "") or ""),
                kind=ModelKind.ASR,
                license=str(getattr(config, "asr_license", "") or "").strip(),
                status=ModelStatus.MISSING,
                path=asr_path or None,
                checksum=str(getattr(config, "asr_checksum", "") or "").strip() or None,
                size_bytes=_optional_int(getattr(config, "asr_size_bytes", None)),
                source="config",
                heavy=bool(getattr(config, "asr_heavy", True)),
                metadata={
                    "backend": str(getattr(config, "asr_backend", "faster-whisper")),
                    "compute_type": str(getattr(config, "asr_compute_type", "int8")),
                    "threads": _optional_int(getattr(config, "asr_threads", None)),
                    "vad_filter": bool(getattr(config, "asr_vad_filter", True)),
                    "beam_size": _optional_int(getattr(config, "asr_beam_size", None)),
                    "repo": asr_model,
                },
            )
        )
    return tuple(specs)


_KNOWN_DEMUCS: dict[str, dict[str, str | int]] = {
    "htdemucs": {
        "version": "955717e8-8726e21a",
        "checksum": ("8726e21a993978c7ba086d3872e7608d7d5bfca646ca4aca459ffda844faa8b4"),
        "size_bytes": 84_141_911,
        "url": ("https://dl.fbaipublicfiles.com/demucs/hybrid_transformer/955717e8-8726e21a.th"),
        "file_name": "955717e8-8726e21a.th",
    }
}


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


__all__ = [
    "ModelInfo",
    "ModelKind",
    "ModelManager",
    "ModelRegistry",
    "ModelStatus",
    "model_specs_from_config",
]
