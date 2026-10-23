"""Stable result DTOs for pipeline runs, publication, and doctor checks.

All DTOs are frozen.  They intentionally expose plain values (strings, tuples,
small mappings) so the CLI, Python API, and future HTTP layer serialize the
same shape without reaching into domain objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from trackforge.application.dto.db import RunItemStatus, RunStatus, StepStatus
from trackforge.application.ports.publishing import (
    DEFAULT_NAMING_TEMPLATE,
    PublishOptions,
)
from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model._validation import coerce_enum, optional_text, utc_now
from trackforge.domain.model.ids import ItemId, RunId
from trackforge.domain.model.item import ItemStatus
from trackforge.domain.model.outputs import (
    LibraryEntry,
    LibraryStatus,
    OutputBundle,
    TargetFormat,
)

_KNOWN_STEPS = frozenset(
    {"plan", "acquire", "observe", "interpret", "resolve", "render", "publish"}
)


def _coerce_target(value: object) -> TargetFormat | None:
    if value is None:
        return None
    if isinstance(value, TargetFormat):
        return value
    if isinstance(value, str):
        try:
            return TargetFormat(value.strip().lower())
        except ValueError:
            pass
    raise DomainValidationError(
        "target_format must be one of: mp3, m4a, flac, ogg, opus",
        context={"target_format": str(value)},
    )


@dataclass(frozen=True, slots=True)
class RunOptions:
    """Per-call overrides for planning, running, and publishing."""

    profile: str = "balanced"
    mode: str = "policy"
    auto: bool = False
    review: bool = False
    force_steps: tuple[str, ...] | str = ()
    offline: bool = False
    dry_run: bool = False
    target_format: TargetFormat | None = None
    naming: str = DEFAULT_NAMING_TEMPLATE
    publish: PublishOptions = field(default_factory=PublishOptions)

    def __post_init__(self) -> None:
        profile = optional_text(self.profile, "profile")
        if profile is None:
            raise DomainValidationError("profile must not be empty")
        mode = optional_text(self.mode, "mode")
        if mode is None:
            raise DomainValidationError("mode must not be empty")
        naming = optional_text(self.naming, "naming")
        if naming is None:
            raise DomainValidationError("naming must not be empty")
        if self.auto and self.review:
            raise DomainValidationError("auto and review are mutually exclusive")
        for name in ("auto", "review", "offline", "dry_run"):
            if not isinstance(getattr(self, name), bool):
                raise DomainValidationError(f"{name} must be a boolean")
        raw_steps = self.force_steps
        if isinstance(raw_steps, str):
            raw_steps = tuple(part.strip() for part in raw_steps.split(",") if part.strip())
        else:
            raw_steps = tuple(str(part).strip() for part in raw_steps if str(part).strip())
        normalized: list[str] = []
        for step_name in raw_steps:
            lowered = step_name.casefold()
            if lowered == "all":
                normalized.extend(sorted(_KNOWN_STEPS))
                continue
            if lowered not in _KNOWN_STEPS:
                raise DomainValidationError(
                    "unknown forced step",
                    context={"step": step_name, "known": sorted(_KNOWN_STEPS)},
                )
            if lowered not in normalized:
                normalized.append(lowered)
        object.__setattr__(self, "profile", profile)
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "naming", naming)
        object.__setattr__(self, "force_steps", tuple(normalized))
        object.__setattr__(self, "target_format", _coerce_target(self.target_format))
        if not isinstance(self.publish, PublishOptions):
            raise DomainValidationError("publish must be a PublishOptions instance")


@dataclass(frozen=True, slots=True)
class RunItemView:
    """One item's outcome inside a run."""

    item_id: ItemId
    status: ItemStatus
    identity_summary: dict[str, Any] = field(default_factory=dict)
    decisions_summary: dict[str, Any] = field(default_factory=dict)
    output_path: str | None = None
    warnings: tuple[str, ...] = ()
    cached_steps: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "status",
            coerce_enum(self.status, ItemStatus, "status"),
        )
        object.__setattr__(self, "identity_summary", dict(self.identity_summary))
        object.__setattr__(self, "decisions_summary", dict(self.decisions_summary))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "cached_steps", tuple(self.cached_steps))

    @property
    def id(self) -> ItemId:
        return self.item_id


@dataclass(frozen=True, slots=True)
class RunStepView:
    """A high-level pipeline step persisted for one run."""

    capability_id: str
    status: StepStatus
    attempts: int = 0
    cache_key: str | None = None
    error: dict[str, Any] | None = None
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "status",
            coerce_enum(self.status, StepStatus, "status"),
        )
        object.__setattr__(self, "warnings", tuple(self.warnings))


@dataclass(frozen=True, slots=True)
class RunResult:
    """The observable result of one import/run/resume operation."""

    run_id: RunId
    status: RunStatus
    items: tuple[RunItemView, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    output_paths: tuple[str, ...] = ()
    steps: tuple[RunStepView, ...] = ()
    cache_hit: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "status",
            coerce_enum(self.status, RunStatus, "status"),
        )
        object.__setattr__(self, "items", tuple(self.items))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "errors", tuple(self.errors))
        object.__setattr__(self, "output_paths", tuple(self.output_paths))
        object.__setattr__(self, "steps", tuple(self.steps))

    @property
    def succeeded(self) -> bool:
        return self.status is RunStatus.COMPLETED


@dataclass(frozen=True, slots=True)
class PublishResult:
    """Published entry plus the concrete sibling files written."""

    entry: LibraryEntry
    cache_hit: bool = False
    sidecar_path: str | None = None
    lrc_path: str | None = None
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "warnings", tuple(self.warnings))

    @property
    def id(self) -> str:
        return str(self.entry.id)

    @property
    def path(self) -> str:
        return self.entry.path

    @property
    def item_id(self) -> ItemId:
        return self.entry.item_id

    @property
    def bundle_id(self) -> str:
        return str(self.entry.bundle_id)

    @property
    def status(self) -> LibraryStatus:
        return self.entry.status

    @property
    def published_at(self) -> datetime:
        return self.entry.published_at


class DoctorStatus(StrEnum):
    """Overall doctor outcome, ordered by severity."""

    OK = "ok"
    WARNING = "warning"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return {DoctorStatus.OK: 0, DoctorStatus.WARNING: 1, DoctorStatus.CRITICAL: 2}[self]


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    """One environment, permission, or configuration check."""

    name: str
    status: DoctorStatus
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise DomainValidationError("doctor check name must be non-empty")
        object.__setattr__(
            self,
            "status",
            coerce_enum(self.status, DoctorStatus, "status"),
        )
        object.__setattr__(self, "details", dict(self.details))


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """The complete doctor report; ``status`` is the worst check."""

    checks: tuple[DoctorCheck, ...] = ()
    generated_at: datetime = field(default_factory=utc_now)
    version: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "checks", tuple(self.checks))

    @property
    def status(self) -> DoctorStatus:
        status = DoctorStatus.OK
        for check in self.checks:
            if check.status.rank > status.rank:
                status = check.status
        return status

    @property
    def critical(self) -> bool:
        return self.status is DoctorStatus.CRITICAL

    @property
    def warnings(self) -> tuple[DoctorCheck, ...]:
        return tuple(check for check in self.checks if check.status is DoctorStatus.WARNING)

    @property
    def failures(self) -> tuple[DoctorCheck, ...]:
        return tuple(check for check in self.checks if check.status is DoctorStatus.CRITICAL)

    @property
    def summary(self) -> str:
        counts = {
            DoctorStatus.OK: 0,
            DoctorStatus.WARNING: 0,
            DoctorStatus.CRITICAL: 0,
        }
        for check in self.checks:
            counts[check.status] += 1
        return (
            f"{counts[DoctorStatus.OK]} ok, "
            f"{counts[DoctorStatus.WARNING]} warning, "
            f"{counts[DoctorStatus.CRITICAL]} critical"
        )


@dataclass(frozen=True, slots=True)
class AnalysisView:
    """Combined observe + interpret result exposed by the Python API."""

    item_id: ItemId
    status: ItemStatus
    observations: Any | None = None
    interpretation: Any | None = None
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "warnings", tuple(self.warnings))


@dataclass(frozen=True, slots=True)
class StatusView:
    """Run status including persisted steps and run items."""

    run_id: RunId
    status: RunStatus
    profile: str
    mode: str
    steps: tuple[RunStepView, ...] = ()
    items: tuple[tuple[ItemId, RunItemStatus], ...] = ()
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "status",
            coerce_enum(self.status, RunStatus, "status"),
        )
        object.__setattr__(self, "steps", tuple(self.steps))
        object.__setattr__(
            self,
            "items",
            tuple(
                (item_id, coerce_enum(status, RunItemStatus, "item_status"))
                for item_id, status in self.items
            ),
        )


@dataclass(frozen=True, slots=True)
class RenderView:
    """Serializable projection of a render result."""

    render_id: str
    bundle_id: str
    bundle_hash: str
    item_id: ItemId
    directory: Path
    files: tuple[str, ...]
    audio_path: Path
    target_format: TargetFormat
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "directory", Path(self.directory))
        object.__setattr__(self, "audio_path", Path(self.audio_path))
        object.__setattr__(
            self,
            "target_format",
            coerce_enum(self.target_format, TargetFormat, "target_format"),
        )
        object.__setattr__(self, "files", tuple(self.files))
        object.__setattr__(self, "warnings", tuple(self.warnings))

    @property
    def path(self) -> Path:
        return self.audio_path

    @property
    def id(self) -> str:
        return self.bundle_id


def render_view(result: Any) -> RenderView:
    """Project a ``RenderResult`` without importing its concrete class."""

    return RenderView(
        render_id=str(result.render.id),
        bundle_id=str(result.bundle.id),
        bundle_hash=result.bundle.bundle_hash,
        item_id=result.render.item_id,
        directory=result.directory,
        files=result.bundle.files,
        audio_path=result.audio_path,
        target_format=result.render.target_format,
        warnings=result.warnings,
    )


def publish_result(
    entry: LibraryEntry,
    *,
    bundle: OutputBundle | None = None,
    cache_hit: bool = False,
    sidecar_path: str | None = None,
    lrc_path: str | None = None,
    warnings: tuple[str, ...] = (),
) -> PublishResult:
    """Build a publish result; ``bundle`` is accepted for call-site symmetry."""

    del bundle
    return PublishResult(
        entry=entry,
        cache_hit=cache_hit,
        sidecar_path=sidecar_path,
        lrc_path=lrc_path,
        warnings=warnings,
    )


from trackforge.application.dto.decisions import ExplanationView  # noqa: E402
from trackforge.application.dto.views import (  # noqa: E402
    DecisionView,
    ItemView,
    LibraryEntryView,
    ResolveView,
    ReviewFieldView,
    ReviewView,
)

__all__ = [
    "AnalysisView",
    "DecisionView",
    "ItemView",
    "LibraryEntryView",
    "ResolveView",
    "ReviewFieldView",
    "ReviewView",
    "DoctorCheck",
    "DoctorReport",
    "DoctorStatus",
    "ExplanationView",
    "PublishResult",
    "RenderView",
    "RunItemView",
    "RunOptions",
    "RunResult",
    "RunStepView",
    "StatusView",
    "publish_result",
    "render_view",
]
