"""Field policies, decisions, and append-only decision records."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime
from enum import StrEnum
from typing import Any

from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model._validation import (
    Array,
    FieldValue,
    coerce_enum,
    ensure_bool,
    ensure_datetime,
    ensure_float,
    ensure_id_tuple,
    ensure_int,
    ensure_sequence,
    ensure_str_tuple,
    json_mapping,
    optional_id,
    optional_text,
    require_id,
    require_text,
    utc_now,
    validate_confidence,
    validate_field_value,
)
from trackforge.domain.model.claims import Subject
from trackforge.domain.model.ids import ClaimId, DecisionId, DecisionRecordId, ItemId

PolicyCondition = str | dict[str, Any]

_UNSET: Any = object()


class DecisionMode(StrEnum):
    POLICY = "policy"
    LLM_ASSISTED = "llm_assisted"
    LLM_AUTOPILOT = "llm_autopilot"
    MANUAL = "manual"


Mode = DecisionMode


class DecidedBy(StrEnum):
    POLICY = "policy"
    POLICY_RANKER = "policy+ranker"
    POLICY_LLM = "policy+llm"
    LLM = "llm"
    USER = "user"
    HYBRID = "hybrid"


class PolicyScope(StrEnum):
    GLOBAL = "global"
    PROFILE = "profile"
    SOURCE = "source"
    ARTIST = "artist"
    ITEM = "item"


FieldScope = PolicyScope


class PolicyStrategy(StrEnum):
    SOURCE_PRIORITY = "source_priority"
    AUTHORITY = "authority"
    CONSENSUS = "consensus"
    LLM_ADJUDICATE = "llm_adjudicate"
    RANKER_ADJUDICATE = "ranker_adjudicate"
    USER_LOCK = "user_lock"
    DERIVE = "derive"
    KEEP_EXISTING = "keep_existing"
    REVIEW = "review"
    UNKNOWN = "unknown"


Strategy = PolicyStrategy


class OnMissing(StrEnum):
    UNKNOWN = "unknown"
    REVIEW = "review"
    FAIL = "fail"


MissingPolicy = OnMissing


class FieldValueClass(StrEnum):
    """Coarse value shape used by policies and validators."""

    STRING = "string"
    TEXT = "string"
    INTEGER = "integer"
    FLOAT = "float"
    NUMBER = "number"
    BOOLEAN = "boolean"
    STRING_LIST = "string_list"
    LIST = "string_list"
    NULL = "null"
    NONE = "null"
    UNKNOWN = "unknown"

    @classmethod
    def of(cls, value: object) -> FieldValueClass:
        if value is None:
            return cls.UNKNOWN
        if isinstance(value, bool):
            return cls.BOOLEAN
        if isinstance(value, str):
            return cls.STRING
        if isinstance(value, int):
            return cls.INTEGER
        if isinstance(value, float):
            return cls.FLOAT
        if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
            return cls.STRING_LIST
        return cls.UNKNOWN


@dataclass(frozen=True, slots=True)
class FieldPolicy:
    """Rules for selecting or deriving one field."""

    field: str
    scope: PolicyScope = PolicyScope.GLOBAL
    conditions: tuple[PolicyCondition, ...] = ()
    strategy: PolicyStrategy = PolicyStrategy.SOURCE_PRIORITY
    priority: tuple[str, ...] = ()
    validators: tuple[str, ...] = ()
    on_missing: OnMissing = OnMissing.UNKNOWN
    version: int = 1
    id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "field", require_text(self.field, "field"))
        object.__setattr__(
            self,
            "scope",
            coerce_enum(self.scope, PolicyScope, "scope"),
        )
        object.__setattr__(
            self,
            "conditions",
            _policy_conditions(self.conditions),
        )
        object.__setattr__(
            self,
            "strategy",
            coerce_enum(self.strategy, PolicyStrategy, "strategy"),
        )
        object.__setattr__(
            self,
            "priority",
            ensure_str_tuple(self.priority, "priority"),
        )
        object.__setattr__(
            self,
            "validators",
            ensure_str_tuple(self.validators, "validators"),
        )
        object.__setattr__(
            self,
            "on_missing",
            coerce_enum(self.on_missing, OnMissing, "on_missing"),
        )
        object.__setattr__(
            self,
            "version",
            ensure_int(self.version, "version", minimum=1),
        )
        object.__setattr__(self, "id", optional_text(self.id, "id"))


def _policy_conditions(value: object) -> tuple[PolicyCondition, ...]:
    items = ensure_sequence(value, "conditions")
    result: list[PolicyCondition] = []
    for index, item in enumerate(items):
        if isinstance(item, str):
            result.append(require_text(item, f"conditions[{index}]"))
        elif isinstance(item, dict):
            result.append(json_mapping(item, f"conditions[{index}]"))
        else:
            raise DomainValidationError(
                f"conditions[{index}] must be a string or JSON object",
                context={"index": index, "actual_type": type(item).__name__},
            )
    return Array(result)


@dataclass(frozen=True, slots=True)
class Decision:
    """One selected (or unknown) field value with alternatives and rationale."""

    field: str
    subject: Subject
    value: FieldValue
    chosen_claim_id: ClaimId | None = None
    alternatives: tuple[ClaimId, ...] = ()
    derived_from: tuple[ClaimId, ...] = ()
    policy_id: str = ""
    mode: DecisionMode = DecisionMode.POLICY
    decided_by: DecidedBy = DecidedBy.POLICY
    confidence: float = _UNSET
    rationale: str = ""
    locked: bool = False
    created_at: datetime = dc_field(default_factory=utc_now)
    id: DecisionId | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "field", require_text(self.field, "field"))
        object.__setattr__(self, "subject", coerce_enum(self.subject, Subject, "subject"))
        object.__setattr__(self, "value", validate_field_value(self.value, "value"))
        object.__setattr__(self, "policy_id", require_text(self.policy_id, "policy_id"))
        if self.confidence is _UNSET:
            raise DomainValidationError(
                "decision confidence is required",
                context={"field": self.field},
            )
        object.__setattr__(
            self,
            "confidence",
            validate_confidence(self.confidence, "confidence"),
        )
        object.__setattr__(self, "rationale", require_text(self.rationale, "rationale"))
        object.__setattr__(
            self,
            "chosen_claim_id",
            optional_id(self.chosen_claim_id, "chosen_claim_id"),
        )
        object.__setattr__(
            self,
            "alternatives",
            ensure_id_tuple(self.alternatives, "alternatives"),
        )
        object.__setattr__(
            self,
            "derived_from",
            ensure_id_tuple(self.derived_from, "derived_from"),
        )
        object.__setattr__(
            self,
            "mode",
            coerce_enum(self.mode, DecisionMode, "mode"),
        )
        object.__setattr__(
            self,
            "decided_by",
            coerce_enum(self.decided_by, DecidedBy, "decided_by"),
        )
        object.__setattr__(self, "locked", ensure_bool(self.locked, "locked"))
        object.__setattr__(
            self,
            "created_at",
            ensure_datetime(self.created_at, "created_at"),
        )
        object.__setattr__(self, "id", optional_id(self.id, "id"))

        if self.value is None and self.chosen_claim_id is not None:
            raise DomainValidationError(
                "an unknown decision cannot choose a claim",
                context={"field": self.field},
            )
        if self.value is not None and self.chosen_claim_id is None and not self.derived_from:
            raise DomainValidationError(
                "a synthesized decision value requires derived_from claim ids",
                context={"field": self.field},
            )

    @property
    def is_unknown(self) -> bool:
        return self.value is None

    @property
    def is_synthesized(self) -> bool:
        return self.chosen_claim_id is None and self.value is not None


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """Append-only snapshot of all decisions for one item."""

    id: DecisionRecordId
    item_id: ItemId
    version: int
    decisions: tuple[Decision, ...] = ()
    policy_versions: tuple[str, ...] = ()
    llm_model: str | None = None
    prompt_version: str | None = None
    input_claim_ids: tuple[ClaimId, ...] = ()
    llm_cost_usd: float | None = None
    llm_usage: dict[str, Any] | None = None
    created_at: datetime = dc_field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", require_id(self.id, "id"))
        object.__setattr__(self, "item_id", require_id(self.item_id, "item_id"))
        object.__setattr__(
            self,
            "version",
            ensure_int(self.version, "version", minimum=1),
        )
        raw_decisions = ensure_sequence(self.decisions, "decisions")
        seen_fields: set[tuple[str, str]] = set()
        decisions: list[Decision] = []
        for index, item in enumerate(raw_decisions):
            if not isinstance(item, Decision):
                raise DomainValidationError(
                    f"decisions[{index}] must be a Decision",
                    context={"index": index},
                )
            key = (item.subject.value, item.field)
            if key in seen_fields:
                raise DomainValidationError(
                    "a decision record may contain only one decision per subject and field",
                    context={"subject": item.subject.value, "field": item.field},
                )
            seen_fields.add(key)
            decisions.append(item)
        object.__setattr__(self, "decisions", Array(decisions))
        object.__setattr__(
            self,
            "policy_versions",
            ensure_str_tuple(self.policy_versions, "policy_versions"),
        )
        object.__setattr__(self, "llm_model", optional_text(self.llm_model, "llm_model"))
        object.__setattr__(
            self,
            "prompt_version",
            optional_text(self.prompt_version, "prompt_version"),
        )
        object.__setattr__(
            self,
            "input_claim_ids",
            ensure_id_tuple(self.input_claim_ids, "input_claim_ids"),
        )
        if self.llm_cost_usd is not None:
            object.__setattr__(
                self,
                "llm_cost_usd",
                ensure_float(self.llm_cost_usd, "llm_cost_usd", minimum=0.0),
            )
        if self.llm_usage is not None:
            object.__setattr__(
                self,
                "llm_usage",
                json_mapping(self.llm_usage, "llm_usage"),
            )
        object.__setattr__(
            self,
            "created_at",
            ensure_datetime(self.created_at, "created_at"),
        )

    def decision_for(
        self,
        field: str,
        *,
        subject: Subject = Subject.ITEM,
    ) -> Decision | None:
        target_subject = coerce_enum(subject, Subject, "subject")
        for decision in self.decisions:
            if decision.field == field and decision.subject is target_subject:
                return decision
        return None


__all__ = [
    "DecidedBy",
    "Decision",
    "DecisionMode",
    "DecisionRecord",
    "FieldPolicy",
    "FieldScope",
    "FieldValueClass",
    "MissingPolicy",
    "Mode",
    "OnMissing",
    "PolicyCondition",
    "PolicyScope",
    "PolicyStrategy",
    "Strategy",
]
