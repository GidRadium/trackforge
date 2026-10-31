"""Capability execution contracts.

A capability is the smallest unit of pipeline work.  It declares what it
needs and produces through a :class:`~trackforge.domain.model.capabilities.CapabilityManifest`
and only exposes a single asynchronous ``run`` method.  Capabilities do not
write to the catalog, choose final tags, or call other capabilities directly;
the pipeline executor owns all state transitions, caching, and persistence.

These types live in the application layer so plugins and concrete analysis
adapters can implement the protocol without importing the orchestration
package.  The pipeline layer depends on this module, never the other way
around.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from trackforge.application.dto.artifacts import ArtifactManifest
from trackforge.application.ports.storage import ArtifactRef as StoredArtifactRef
from trackforge.application.services.canonical_json import canonical_json
from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model._validation import (
    coerce_enum,
    ensure_bool,
    ensure_datetime,
    ensure_float,
    json_mapping,
    optional_text,
    require_text,
    utc_now,
)
from trackforge.domain.model.capabilities import ArtifactType, CapabilityManifest, Cost
from trackforge.domain.model.ids import ItemId, RunId, StepId, new_uuid7


class StepResultStatus(StrEnum):
    """Outcome reported by a capability for one successful invocation."""

    SUCCEEDED = "succeeded"
    DEGRADED = "degraded"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class StepWarning:
    """A visible, structured degradation warning.

    ``code`` intentionally stays a plain string so plugins can introduce
    domain-specific codes while the engine's standard codes live in
    :mod:`trackforge.pipeline.degradation`.
    """

    code: str
    message: str
    capability_id: str | None = None
    provider: str | None = None
    retryable: bool = False
    user_action: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.code, StrEnum):
            object.__setattr__(self, "code", self.code.value)
        object.__setattr__(self, "code", require_text(self.code, "code"))
        object.__setattr__(self, "message", require_text(self.message, "message"))
        if self.capability_id is not None:
            object.__setattr__(
                self,
                "capability_id",
                require_text(self.capability_id, "capability_id"),
            )
        if self.provider is not None:
            object.__setattr__(self, "provider", require_text(self.provider, "provider"))
        object.__setattr__(self, "retryable", ensure_bool(self.retryable, "retryable"))
        if self.user_action is not None:
            object.__setattr__(
                self,
                "user_action",
                require_text(self.user_action, "user_action"),
            )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation for catalog warnings."""

        return {
            "code": self.code,
            "message": self.message,
            "capability_id": self.capability_id,
            "provider": self.provider,
            "retryable": self.retryable,
            "user_action": self.user_action,
        }


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    """Remaining budgets visible to a running capability.

    ``None`` means the corresponding resource is unlimited.  The executor
    snapshots a :class:`trackforge.pipeline.budget.Budget` before each attempt;
    capabilities may use this only to choose a cheaper path, never to mutate
    the authoritative accounting.
    """

    cpu_seconds: float | None = None
    memory_mb: float | None = None
    network_calls: int | None = None
    money_usd: float | None = None

    def __post_init__(self) -> None:
        for name in ("cpu_seconds", "memory_mb", "money_usd"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(
                    self,
                    name,
                    ensure_float(value, name, minimum=0.0),
                )
        if self.network_calls is not None:
            calls = self.network_calls
            if isinstance(calls, bool) or not isinstance(calls, int) or calls < 0:
                raise DomainValidationError(
                    "network_calls must be a non-negative integer or None",
                    context={"value": str(calls)},
                )

    @property
    def exhausted(self) -> bool:
        """Return whether any finite budget is fully consumed."""

        return (
            self.cpu_seconds == 0.0
            or self.memory_mb == 0.0
            or self.network_calls == 0
            or self.money_usd == 0.0
        )


@dataclass(frozen=True, slots=True)
class StepInput:
    """One resolved input artifact handed to a capability."""

    type: ArtifactType
    artifact_id: str
    schema_version: int
    cache_key: str
    payload_hash: str
    payload: bytes
    manifest: ArtifactManifest | None = None
    ref: StoredArtifactRef | None = None

    def __post_init__(self) -> None:
        if isinstance(self.type, str):  # type: ignore[unreachable]
            object.__setattr__(  # type: ignore[unreachable]
                self, "type", ArtifactType.parse(self.type)
            )
        elif not isinstance(self.type, ArtifactType):
            raise DomainValidationError("type must be an ArtifactType")
        object.__setattr__(self, "artifact_id", require_text(self.artifact_id, "artifact_id"))
        if isinstance(self.schema_version, bool) or not isinstance(self.schema_version, int):
            raise DomainValidationError("schema_version must be an integer")
        if self.schema_version < 1:
            raise DomainValidationError("schema_version must be >= 1")
        object.__setattr__(self, "cache_key", require_text(self.cache_key, "cache_key"))
        object.__setattr__(
            self,
            "payload_hash",
            require_text(self.payload_hash, "payload_hash"),
        )
        if not isinstance(self.payload, (bytes, bytearray, memoryview)):
            raise DomainValidationError("payload must be bytes")
        object.__setattr__(self, "payload", bytes(self.payload))


@dataclass(frozen=True, slots=True)
class ArtifactDraft:
    """An artifact produced by a capability, not yet persisted.

    JSON payloads are validated for canonical serialization at construction
    time.  Binary drafts must carry ``payload_format='bin'`` and ``bytes``.
    """

    type: ArtifactType
    payload: Any = None
    payload_format: Literal["json", "bin"] = "json"
    params: Mapping[str, Any] = field(default_factory=dict)
    warnings: tuple[StepWarning, ...] = ()
    expires_at: datetime | None = None
    artifact_id: str = field(default_factory=new_uuid7)

    def __post_init__(self) -> None:
        if isinstance(self.type, str):  # type: ignore[unreachable]
            object.__setattr__(  # type: ignore[unreachable]
                self, "type", ArtifactType.parse(self.type)
            )
        elif not isinstance(self.type, ArtifactType):
            raise DomainValidationError("type must be an ArtifactType")
        if self.payload_format not in ("json", "bin"):
            raise DomainValidationError("payload_format must be 'json' or 'bin'")
        if self.payload_format == "json":
            try:
                canonical_json(self.payload)
            except (TypeError, ValueError) as exc:
                raise DomainValidationError(
                    f"JSON artifact payload is not canonical-JSON safe: {exc}",
                    context={"type": str(self.type)},
                ) from exc
        elif not isinstance(self.payload, (bytes, bytearray, memoryview)):
            raise DomainValidationError("binary artifact payload must be bytes")
        else:
            object.__setattr__(self, "payload", bytes(self.payload))
        object.__setattr__(self, "params", json_mapping(self.params, "params"))
        coerced: list[StepWarning] = []
        for index, warning in enumerate(tuple(self.warnings)):
            if isinstance(warning, StepWarning):
                coerced.append(warning)
            elif isinstance(warning, str):  # type: ignore[unreachable]
                coerced.append(StepWarning(code=warning, message=warning))
            else:
                raise DomainValidationError(f"warnings[{index}] must be a StepWarning or string")
        object.__setattr__(self, "warnings", tuple(coerced))
        if self.expires_at is not None:
            expires = ensure_datetime(self.expires_at, "expires_at")
            object.__setattr__(
                self,
                "expires_at",
                expires.replace(tzinfo=UTC) if expires.tzinfo is None else expires.astimezone(UTC),
            )
        object.__setattr__(self, "artifact_id", require_text(self.artifact_id, "artifact_id"))

    @property
    def is_json(self) -> bool:
        return self.payload_format == "json"


@dataclass(frozen=True, slots=True)
class StepResult:
    """The only value a capability returns to the executor."""

    artifacts: tuple[ArtifactDraft, ...] = ()
    warnings: tuple[StepWarning, ...] = ()
    actual_cost: Cost = field(default_factory=Cost)
    status: StepResultStatus = StepResultStatus.SUCCEEDED
    summary: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        artifacts = tuple(self.artifacts)
        for index, artifact in enumerate(artifacts):
            if not isinstance(artifact, ArtifactDraft):
                raise DomainValidationError(f"artifacts[{index}] must be an ArtifactDraft")
        object.__setattr__(self, "artifacts", artifacts)

        raw_warnings = tuple(self.warnings)
        warnings: list[StepWarning] = []
        for index, warning in enumerate(raw_warnings):
            if isinstance(warning, StepWarning):
                warnings.append(warning)
            elif isinstance(warning, str):  # type: ignore[unreachable]
                warnings.append(StepWarning(code=warning, message=warning))
            else:
                raise DomainValidationError(f"warnings[{index}] must be a StepWarning or string")
        object.__setattr__(self, "warnings", tuple(warnings))
        if not isinstance(self.actual_cost, Cost):
            raise DomainValidationError("actual_cost must be a Cost")
        object.__setattr__(
            self,
            "status",
            coerce_enum(self.status, StepResultStatus, "status"),
        )
        object.__setattr__(self, "summary", optional_text(self.summary, "summary"))
        object.__setattr__(self, "details", json_mapping(self.details, "details"))

    @classmethod
    def skipped(
        cls,
        *,
        warnings: tuple[StepWarning, ...] = (),
        actual_cost: Cost | None = None,
        summary: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> StepResult:
        """Build a skipped result without artifacts."""

        return cls(
            warnings=warnings,
            actual_cost=actual_cost or Cost(),
            status=StepResultStatus.SKIPPED,
            summary=summary,
            details=dict(details or {}),
        )

    @classmethod
    def degraded(
        cls,
        *,
        artifacts: tuple[ArtifactDraft, ...] = (),
        warnings: tuple[StepWarning, ...] = (),
        actual_cost: Cost | None = None,
        summary: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> StepResult:
        """Build a degraded but usable result."""

        return cls(
            artifacts=artifacts,
            warnings=warnings,
            actual_cost=actual_cost or Cost(),
            status=StepResultStatus.DEGRADED,
            summary=summary,
            details=dict(details or {}),
        )

    @property
    def warning_codes(self) -> tuple[str, ...]:
        return tuple(warning.code for warning in self.warnings)


@dataclass(frozen=True, slots=True)
class StepContext:
    """Everything a capability may read or report for one attempt."""

    run_id: RunId
    item_id: ItemId
    step_id: StepId
    inputs: Mapping[ArtifactType, StepInput] = field(default_factory=dict)
    params: Mapping[str, Any] = field(default_factory=dict)
    config_subset: Mapping[str, Any] = field(default_factory=dict)
    budget: BudgetSnapshot = field(default_factory=BudgetSnapshot)
    force: bool = False
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    report_progress: Callable[[float, str | None], None] | None = None
    report_message: Callable[[str | None], None] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "inputs", dict(self.inputs))
        object.__setattr__(self, "params", json_mapping(self.params, "params"))
        object.__setattr__(
            self,
            "config_subset",
            json_mapping(self.config_subset, "config_subset"),
        )
        object.__setattr__(self, "force", ensure_bool(self.force, "force"))
        if not isinstance(self.cancelled, asyncio.Event):
            raise DomainValidationError("cancelled must be an asyncio.Event")

    @property
    def is_cancelled(self) -> bool:
        return self.cancelled.is_set()

    def progress(self, fraction: float, message: str | None = None) -> None:
        """Report progress in ``[0, 1]`` if the executor supplied a callback."""

        value = max(0.0, min(1.0, float(fraction)))
        if self.report_progress is not None:
            self.report_progress(value, message)

    def note(self, message: str | None) -> None:
        """Report a stage note without inventing a measured percentage."""

        if self.report_message is not None:
            self.report_message(message)


@runtime_checkable
class Capability(Protocol):
    """A manifest-declared unit of pipeline work."""

    @property
    def manifest(self) -> CapabilityManifest:
        """The capability contract used by the planner and cache."""

        ...

    async def run(self, ctx: StepContext) -> StepResult:
        """Perform the work for one attempt and return artifacts/warnings."""

        ...


def utc_timestamp() -> datetime:
    """Return an aware UTC timestamp (kept here for plugin authors)."""

    return utc_now()


__all__ = [
    "ArtifactDraft",
    "BudgetSnapshot",
    "Capability",
    "StepContext",
    "StepInput",
    "StepResult",
    "StepResultStatus",
    "StepWarning",
    "utc_timestamp",
]
