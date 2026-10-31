"""Capability manifests, artifact types, costs, and analysis profiles."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model._validation import (
    Array,
    coerce_enum,
    ensure_bool,
    ensure_float,
    ensure_int,
    ensure_sequence,
    ensure_str_tuple,
    json_mapping,
    optional_text,
    require_text,
)

_NAMESPACE_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_ARTIFACT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+\.v[1-9][0-9]*$")
_ARTIFACT_TYPE_PARTS_RE = re.compile(
    r"^(?P<namespace>[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*)\."
    r"(?P<name>[a-z][a-z0-9_]*)\.v(?P<version>[1-9][0-9]*)$"
)


@dataclass(frozen=True, slots=True)
class ArtifactType:
    """A namespaced artifact type with a positive schema version."""

    namespace: str
    name: str
    schema_version: int

    def __post_init__(self) -> None:
        if not isinstance(self.namespace, str) or not _NAMESPACE_RE.fullmatch(self.namespace):
            raise DomainValidationError(
                "artifact namespace must be lowercase dot-separated identifiers",
                context={"namespace": str(self.namespace)},
            )
        if not isinstance(self.name, str) or not _NAME_RE.fullmatch(self.name):
            raise DomainValidationError(
                "artifact name must be a lowercase identifier",
                context={"name": str(self.name)},
            )
        object.__setattr__(
            self,
            "schema_version",
            ensure_int(self.schema_version, "schema_version", minimum=1),
        )
        rendered = f"{self.namespace}.{self.name}.v{self.schema_version}"
        if not _ARTIFACT_TYPE_RE.fullmatch(rendered):
            raise DomainValidationError(
                "artifact type does not match the required format",
                context={"artifact_type": rendered},
            )

    def __str__(self) -> str:
        return f"{self.namespace}.{self.name}.v{self.schema_version}"

    def render(self) -> str:
        return str(self)

    @classmethod
    def parse(cls, value: str) -> ArtifactType:
        if not isinstance(value, str):
            raise DomainValidationError(
                "artifact type must be a string",
                context={"actual_type": type(value).__name__},
            )
        match = _ARTIFACT_TYPE_PARTS_RE.fullmatch(value)
        if match is None:
            raise DomainValidationError(
                "artifact type must look like 'namespace.name.vN'",
                context={"value": value},
            )
        return cls(
            namespace=match.group("namespace"),
            name=match.group("name"),
            schema_version=int(match.group("version")),
        )

    @classmethod
    def from_string(cls, value: str) -> ArtifactType:
        return cls.parse(value)

    @classmethod
    def from_str(cls, value: str) -> ArtifactType:
        return cls.parse(value)


@dataclass(frozen=True, slots=True)
class Cost:
    """Estimated or actual resource cost; all values are non-negative."""

    cpu_seconds: float = 0.0
    memory_mb: float = 0.0
    network_calls: int = 0
    money_usd: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "cpu_seconds",
            ensure_float(self.cpu_seconds, "cpu_seconds", minimum=0.0),
        )
        object.__setattr__(
            self,
            "memory_mb",
            ensure_float(self.memory_mb, "memory_mb", minimum=0.0),
        )
        object.__setattr__(
            self,
            "network_calls",
            ensure_int(self.network_calls, "network_calls", minimum=0),
        )
        object.__setattr__(
            self,
            "money_usd",
            ensure_float(self.money_usd, "money_usd", minimum=0.0),
        )

    def __add__(self, other: Cost) -> Cost:
        if not isinstance(other, Cost):
            return NotImplemented
        return Cost(
            cpu_seconds=self.cpu_seconds + other.cpu_seconds,
            memory_mb=self.memory_mb + other.memory_mb,
            network_calls=self.network_calls + other.network_calls,
            money_usd=self.money_usd + other.money_usd,
        )

    @classmethod
    def zero(cls) -> Cost:
        return cls()


def _coerce_cost(value: Cost | Mapping[str, Any]) -> Cost:
    if isinstance(value, Cost):
        return value
    if not isinstance(value, Mapping):
        raise DomainValidationError(
            "cost/budget must be a Cost or a mapping",
            context={"actual_type": type(value).__name__},
        )
    return Cost(
        cpu_seconds=value.get("cpu_seconds", value.get("max_cpu_seconds", 0.0)),
        memory_mb=value.get("memory_mb", value.get("max_memory_mb", 0.0)),
        network_calls=value.get("network_calls", value.get("max_network_calls", 0)),
        money_usd=value.get("money_usd", value.get("max_cost", 0.0)),
    )


class Determinism(StrEnum):
    DETERMINISTIC = "deterministic"
    SEEDED = "seeded"
    NON_DETERMINISTIC = "non_deterministic"


class CapabilityKind(StrEnum):
    OBSERVER = "observer"
    INTERPRETER = "interpreter"
    PROVIDER = "provider"
    RANKER = "ranker"
    RENDERER = "renderer"


class SideEffect(StrEnum):
    NETWORK = "network"
    DISK = "disk"
    NONE = "none"


ArtifactRef = ArtifactType | str


@dataclass(frozen=True, slots=True)
class CapabilityManifest:
    """The contract used by the planner to build and validate a DAG."""

    id: str
    version: str
    kind: CapabilityKind
    requires: tuple[ArtifactRef, ...] = ()
    provides: tuple[ArtifactRef, ...] = ()
    cost: Cost | Mapping[str, Any] = field(default_factory=Cost)
    determinism: Determinism = Determinism.DETERMINISTIC
    cacheable: bool = True
    side_effects: SideEffect = SideEffect.NONE
    rate_limit: str | dict[str, Any] | None = None
    config_schema: dict[str, Any] = field(default_factory=dict)
    license: str | None = None
    description: str | None = None
    optional_requires: tuple[ArtifactRef, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", require_text(self.id, "id"))
        object.__setattr__(self, "version", require_text(self.version, "version"))
        object.__setattr__(
            self,
            "kind",
            coerce_enum(self.kind, CapabilityKind, "kind"),
        )
        object.__setattr__(
            self,
            "requires",
            _artifact_refs(self.requires, "requires"),
        )
        object.__setattr__(
            self,
            "optional_requires",
            _artifact_refs(self.optional_requires, "optional_requires"),
        )
        required_keys = {str(item) for item in self.requires}
        optional_keys = {str(item) for item in self.optional_requires}
        if required_keys.intersection(optional_keys):
            raise DomainValidationError(
                "requires and optional_requires must not overlap",
                context={
                    "overlap": sorted(required_keys.intersection(optional_keys)),
                },
            )
        object.__setattr__(
            self,
            "provides",
            _artifact_refs(self.provides, "provides"),
        )
        object.__setattr__(self, "cost", _coerce_cost(self.cost))
        object.__setattr__(
            self,
            "determinism",
            coerce_enum(self.determinism, Determinism, "determinism"),
        )
        object.__setattr__(
            self,
            "cacheable",
            ensure_bool(self.cacheable, "cacheable"),
        )
        object.__setattr__(
            self,
            "side_effects",
            coerce_enum(self.side_effects, SideEffect, "side_effects"),
        )
        if isinstance(self.rate_limit, Mapping):
            object.__setattr__(
                self,
                "rate_limit",
                json_mapping(self.rate_limit, "rate_limit"),
            )
        else:
            object.__setattr__(
                self,
                "rate_limit",
                optional_text(self.rate_limit, "rate_limit"),
            )
        object.__setattr__(
            self,
            "config_schema",
            json_mapping(self.config_schema, "config_schema"),
        )
        object.__setattr__(self, "license", optional_text(self.license, "license"))
        object.__setattr__(
            self,
            "description",
            optional_text(self.description, "description"),
        )


def _artifact_refs(value: object, field: str) -> tuple[ArtifactRef, ...]:
    items = ensure_sequence(value, field)
    result: list[ArtifactRef] = []
    for index, item in enumerate(items):
        if isinstance(item, ArtifactType):
            result.append(item)
        elif isinstance(item, str):
            result.append(require_text(item, f"{field}[{index}]"))
        else:
            raise DomainValidationError(
                f"{field}[{index}] must be an ArtifactType or string",
                context={"field": field, "index": index},
            )
    return Array(result)


class OnEmpty(StrEnum):
    FAIL = "fail"
    DEGRADE = "degrade"
    REVIEW = "review"


@dataclass(frozen=True, slots=True)
class ProviderChain:
    """Ordered providers for one artifact type."""

    artifact_type: ArtifactType | str
    providers: tuple[str, ...]
    on_empty: OnEmpty = OnEmpty.FAIL

    def __post_init__(self) -> None:
        if isinstance(self.artifact_type, str):
            object.__setattr__(
                self,
                "artifact_type",
                ArtifactType.parse(self.artifact_type),
            )
        elif not isinstance(self.artifact_type, ArtifactType):
            raise DomainValidationError("artifact_type must be an ArtifactType")
        object.__setattr__(
            self,
            "providers",
            ensure_str_tuple(self.providers, "providers", allow_empty=False),
        )
        object.__setattr__(
            self,
            "on_empty",
            coerce_enum(self.on_empty, OnEmpty, "on_empty"),
        )


class SelectionStrategy(StrEnum):
    LLM = "llm"
    RANKER = "ranker"
    LEXICAL = "lexical"
    HYBRID = "hybrid"


@dataclass(frozen=True, slots=True)
class AnalysisProfile:
    """Enabled capabilities, budget, and quality/fallback preferences."""

    name: str
    enabled: tuple[str, ...] = ()
    disabled: tuple[str, ...] = ()
    budget: Cost | Mapping[str, Any] = field(default_factory=Cost)
    quality_target: float | str | None = None
    fallback_policy: str | None = None
    selection_strategy: SelectionStrategy = SelectionStrategy.HYBRID

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", require_text(self.name, "name"))
        enabled = ensure_str_tuple(self.enabled, "enabled")
        disabled = ensure_str_tuple(self.disabled, "disabled")
        overlap = set(enabled) & set(disabled)
        if overlap:
            raise DomainValidationError(
                "enabled and disabled capabilities must not overlap",
                context={"overlap": sorted(overlap)},
            )
        object.__setattr__(self, "enabled", enabled)
        object.__setattr__(self, "disabled", disabled)
        object.__setattr__(self, "budget", _coerce_cost(self.budget))
        if self.quality_target is not None and not isinstance(self.quality_target, str):
            object.__setattr__(
                self,
                "quality_target",
                ensure_float(
                    self.quality_target,
                    "quality_target",
                    minimum=0.0,
                    maximum=1.0,
                ),
            )
        elif self.quality_target is not None:
            object.__setattr__(
                self,
                "quality_target",
                require_text(self.quality_target, "quality_target"),
            )
        object.__setattr__(
            self,
            "fallback_policy",
            optional_text(self.fallback_policy, "fallback_policy"),
        )
        object.__setattr__(
            self,
            "selection_strategy",
            coerce_enum(self.selection_strategy, SelectionStrategy, "selection_strategy"),
        )


__all__ = [
    "AnalysisProfile",
    "ArtifactRef",
    "ArtifactType",
    "CapabilityKind",
    "CapabilityManifest",
    "Cost",
    "Determinism",
    "OnEmpty",
    "ProviderChain",
    "SelectionStrategy",
    "SideEffect",
]
