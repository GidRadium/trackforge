"""Provider-neutral LLM request/response contract.

LLMs are claim extractors and adjudicators, never sources of truth.  The port
keeps provider details (HTTP, credentials, prompt files) out of the
application layer while preserving strict output validation and cost metadata.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from trackforge.domain.errors import (
    BudgetExceeded,
    DomainValidationError,
    LLMConfigurationError,
    LLMInvalidOutput,
    ProviderUnavailable,
    RateLimited,
)
from trackforge.domain.model._validation import (
    FieldValue,
    ensure_float,
    ensure_int,
    json_mapping,
    optional_text,
    require_text,
    validate_field_value,
)


class LLMTask(StrEnum):
    """Supported LLM call sites."""

    EXTRACT = "extract"
    SOURCE_EXTRACT = "source_extract"
    ADJUDICATE = "adjudicate"
    AUTOPILOT = "autopilot"


@dataclass(frozen=True, slots=True)
class SourceSnippet:
    """One untrusted source text fragment sent to the model."""

    text: str
    source: str
    label: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", require_text(self.text, "text"))
        object.__setattr__(self, "source", require_text(self.source, "source"))
        object.__setattr__(self, "label", optional_text(self.label, "label"))

    def as_dict(self) -> dict[str, str]:
        document = {"text": self.text, "source": self.source}
        if self.label is not None:
            document["label"] = self.label
        return document


@dataclass(frozen=True, slots=True)
class LLMClaim:
    """One claim proposed by the model with its evidence."""

    field: str
    value: FieldValue
    confidence: float
    evidence_claim_ids: tuple[str, ...] = ()
    quote: str | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "field", require_text(self.field, "field"))
        object.__setattr__(self, "value", validate_field_value(self.value, "value"))
        object.__setattr__(
            self,
            "confidence",
            ensure_float(self.confidence, "confidence", minimum=0.0, maximum=1.0),
        )
        object.__setattr__(
            self,
            "evidence_claim_ids",
            _text_tuple(self.evidence_claim_ids, "evidence_claim_ids"),
        )
        object.__setattr__(self, "quote", optional_text(self.quote, "quote"))
        object.__setattr__(self, "reason", _optional_text(self.reason, "reason"))


@dataclass(frozen=True, slots=True)
class LLMDecision:
    """One field decision selected or synthesized from input claims."""

    field: str
    value: FieldValue
    chosen_claim_ids: tuple[str, ...] = ()
    derived_from: tuple[str, ...] = ()
    confidence: float = 0.5
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "field", require_text(self.field, "field"))
        object.__setattr__(self, "value", validate_field_value(self.value, "value"))
        object.__setattr__(
            self,
            "chosen_claim_ids",
            _text_tuple(self.chosen_claim_ids, "chosen_claim_ids"),
        )
        object.__setattr__(
            self,
            "derived_from",
            _text_tuple(self.derived_from, "derived_from"),
        )
        object.__setattr__(
            self,
            "confidence",
            ensure_float(self.confidence, "confidence", minimum=0.0, maximum=1.0),
        )
        object.__setattr__(self, "reason", _optional_text(self.reason, "reason"))


@dataclass(frozen=True, slots=True)
class LLMUsage(Mapping[str, int]):
    """Provider-reported token usage.

    Values are optional because a compatible endpoint may omit usage; the
    application never estimates tokens and calls them actual.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None

    def __post_init__(self) -> None:
        for name in ("input_tokens", "output_tokens"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(
                    self,
                    name,
                    ensure_int(value, name, minimum=0),
                )

    def __getitem__(self, key: str) -> int:
        values = self.as_dict()
        if key not in values:
            raise KeyError(key)
        return values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.as_dict())

    def __len__(self) -> int:
        return len(self.as_dict())

    @property
    def total_tokens(self) -> int | None:
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return self.input_tokens + self.output_tokens

    def as_dict(self) -> dict[str, int]:
        result: dict[str, int] = {}
        if self.input_tokens is not None:
            result["input_tokens"] = self.input_tokens
        if self.output_tokens is not None:
            result["output_tokens"] = self.output_tokens
        return result


@dataclass(frozen=True, slots=True)
class LLMRequest:
    """Everything one provider call needs, including untrusted text."""

    task: LLMTask
    schema: dict[str, Any] = field(default_factory=dict)
    claims: tuple[dict[str, Any], ...] = ()
    source_snippets: tuple[SourceSnippet, ...] = ()
    target_fields: tuple[str, ...] = ()
    max_tokens: int = 2_000
    temperature: float = 0.1
    max_cost_usd: float | None = None

    def __post_init__(self) -> None:
        task = self.task if isinstance(self.task, LLMTask) else LLMTask(str(self.task))
        object.__setattr__(self, "task", task)
        object.__setattr__(self, "schema", json_mapping(self.schema, "schema"))
        object.__setattr__(self, "claims", _claim_dicts(self.claims))
        raw_snippets: tuple[object, ...] = tuple(self.source_snippets)
        normalized_snippets: list[SourceSnippet] = []
        for item in raw_snippets:
            if isinstance(item, SourceSnippet):
                normalized_snippets.append(item)
            elif isinstance(item, Mapping):
                normalized_snippets.append(
                    SourceSnippet(
                        text=str(item.get("text", "")),
                        source=str(item.get("source", "")),
                        label=str(item["label"]) if item.get("label") else None,
                    )
                )
            else:
                raise DomainValidationError(
                    "source_snippets must contain SourceSnippet values or JSON objects"
                )
        object.__setattr__(self, "source_snippets", tuple(normalized_snippets))
        object.__setattr__(
            self,
            "target_fields",
            _text_tuple(self.target_fields, "target_fields"),
        )
        object.__setattr__(
            self,
            "max_tokens",
            ensure_int(self.max_tokens, "max_tokens", minimum=1),
        )
        object.__setattr__(
            self,
            "temperature",
            ensure_float(self.temperature, "temperature", minimum=0.0, maximum=2.0),
        )
        if self.max_cost_usd is not None:
            object.__setattr__(
                self,
                "max_cost_usd",
                ensure_float(self.max_cost_usd, "max_cost_usd", minimum=0.0),
            )


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """Strictly parsed provider output plus model/usage/cost provenance."""

    claims: tuple[LLMClaim, ...] = ()
    decisions: tuple[LLMDecision, ...] = ()
    unknown: tuple[str, ...] = ()
    model: str = ""
    prompt_version: str = ""
    usage: LLMUsage = field(default_factory=LLMUsage)
    cost_usd: float | None = None
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        claims = tuple(self.claims)
        decisions = tuple(self.decisions)
        if any(not isinstance(item, LLMClaim) for item in claims):
            raise DomainValidationError("claims must contain LLMClaim values")
        if any(not isinstance(item, LLMDecision) for item in decisions):
            raise DomainValidationError("decisions must contain LLMDecision values")
        object.__setattr__(self, "claims", claims)
        object.__setattr__(self, "decisions", decisions)
        object.__setattr__(self, "unknown", _text_tuple(self.unknown, "unknown"))
        object.__setattr__(self, "model", require_text(self.model, "model"))
        object.__setattr__(
            self,
            "prompt_version",
            require_text(self.prompt_version, "prompt_version"),
        )
        usage_value: object = self.usage
        if not isinstance(usage_value, LLMUsage):
            getter = getattr(usage_value, "get", None)
            if not callable(getter):
                raise DomainValidationError("usage must be an LLMUsage or JSON object")
            object.__setattr__(
                self,
                "usage",
                LLMUsage(
                    input_tokens=getter("input_tokens"),
                    output_tokens=getter("output_tokens"),
                ),
            )
        if self.cost_usd is not None:
            cost = ensure_float(self.cost_usd, "cost_usd", minimum=0.0)
            if not math.isfinite(cost):  # pragma: no cover - ensure_float covers this
                raise DomainValidationError("cost_usd must be finite")
            object.__setattr__(self, "cost_usd", cost)
        object.__setattr__(self, "warnings", _text_tuple(self.warnings, "warnings"))


@runtime_checkable
class LLMProvider(Protocol):
    """One OpenAI-compatible (or fake) completion provider."""

    async def complete(self, request: LLMRequest) -> LLMResponse: ...


def _optional_text(value: object, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise DomainValidationError(
            f"{field} must be a string",
            context={"field": field, "actual_type": type(value).__name__},
        )
    return value if value.strip() else ""


def _text_tuple(value: object, field: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)):
        raise DomainValidationError(f"{field} must be a sequence of strings")
    try:
        items: tuple[object, ...] = tuple(value)  # type: ignore[arg-type]
    except TypeError:
        raise DomainValidationError(f"{field} must be a sequence of strings") from None
    result: list[str] = []
    for item in items:
        result.append(require_text(item, field))
    return tuple(result)


def _claim_dicts(value: object) -> tuple[dict[str, Any], ...]:
    if isinstance(value, (str, bytes, bytearray, Mapping)):
        raise DomainValidationError("claims must be a sequence of JSON objects")
    try:
        items: tuple[object, ...] = tuple(value)  # type: ignore[arg-type]
    except TypeError:
        raise DomainValidationError("claims must be a sequence of JSON objects") from None
    result: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise DomainValidationError(
                "claims must be JSON objects",
                context={"index": index},
            )
        result.append(dict(item))
    return tuple(result)


__all__ = [
    "BudgetExceeded",
    "LLMClaim",
    "LLMConfigurationError",
    "LLMDecision",
    "LLMInvalidOutput",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "LLMTask",
    "LLMUsage",
    "ProviderUnavailable",
    "RateLimited",
    "SourceSnippet",
]
