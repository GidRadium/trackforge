"""Deterministic guardrails for untrusted LLM output.

The provider validates its own envelope; these checks decide whether proposed
claims/decisions may influence resolution.  In particular, models may only
select existing MBID/ISRC/URL values, synthesis needs ``derived_from``, and
autopilot is restricted to a configured field set.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from trackforge.application.ports.llm import LLMClaim, LLMDecision, LLMResponse
from trackforge.domain.errors import BudgetExceeded, LLMInvalidOutput
from trackforge.domain.model._validation import FieldValue
from trackforge.domain.model.claims import Claim
from trackforge.domain.model.decisions import DecisionMode
from trackforge.domain.policies.normalization import (
    canonical_field,
    claim_fields_for,
    value_equal,
)
from trackforge.domain.policies.validators import validate_field

ID_FIELDS = frozenset(
    {
        "mb_recording_id",
        "mb_release_id",
        "mb_release_group_id",
        "mb_artist_id",
        "mb_albumartist_id",
        "isrc",
        "acoustid",
    }
)
AUTOPILOT_DEFAULT_FIELDS = frozenset(
    {
        "title",
        "artist",
        "album",
        "albumartist",
        "date",
        "genre",
        "track",
        "disc",
        "comment",
        "artwork",
    }
)
_ID_FIELD_ALIASES = {
    "recording.mbid": "mb_recording_id",
    "release.mbid": "mb_release_id",
    "release_group.mbid": "mb_release_group_id",
    "artist.mbid": "mb_artist_id",
    "albumartist.mbid": "mb_albumartist_id",
    "recording.isrc": "isrc",
}
_SUBJECT_FIELD_ALIASES = {
    "recording.title": "title",
    "recording.artist": "artist",
    "release.title": "album",
    "release.date": "date",
    "artist.name": "artist",
    "item.title": "title",
    "item.artist": "artist",
    "item.album": "album",
    "item.date": "date",
    "item.genre": "genre",
    "item.artwork": "artwork",
    "item.comment": "comment",
}
_MBID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_ISRC_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}\d{7}$")
_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class GuardrailRejection:
    """One rejected proposal with a stable code for tests/logs."""

    field: str
    code: str
    message: str
    value: FieldValue = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "field", canonical_field(self.field))
        if not self.code or not self.message:
            raise ValueError("guardrail rejection needs a code and message")

    @property
    def warning(self) -> str:
        return f"llm:{self.field}:{self.code}: {self.message}"


@dataclass(frozen=True, slots=True)
class GuardrailResult:
    """Accepted proposals plus visible rejection/degradation warnings."""

    claims: tuple[LLMClaim, ...] = ()
    decisions: tuple[LLMDecision, ...] = ()
    rejections: tuple[GuardrailRejection, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "claims", tuple(self.claims))
        object.__setattr__(self, "decisions", tuple(self.decisions))
        object.__setattr__(self, "rejections", tuple(self.rejections))
        object.__setattr__(self, "warnings", tuple(self.warnings))

    @property
    def all_warnings(self) -> tuple[str, ...]:
        return self.warnings + tuple(rejection.warning for rejection in self.rejections)

    @property
    def ok(self) -> bool:
        return not self.rejections

    @property
    def rejected(self) -> tuple[GuardrailRejection, ...]:
        return self.rejections


@dataclass(frozen=True, slots=True)
class _ClaimIndex:
    by_id: dict[str, Claim]
    by_field: dict[str, tuple[Claim, ...]]

    @classmethod
    def build(cls, claims: Sequence[Claim]) -> _ClaimIndex:
        by_id = {str(claim.id): claim for claim in claims}
        by_field: dict[str, list[Claim]] = {}
        for claim in claims:
            by_field.setdefault(canonical_field(claim.field), []).append(claim)
        return cls(
            by_id=by_id,
            by_field={key: tuple(value) for key, value in by_field.items()},
        )

    def values(self, field: str) -> tuple[FieldValue, ...]:
        canonical = canonical_field(field)
        names = {canonical, *claim_fields_for(canonical)}
        result: list[FieldValue] = []
        for name in names:
            for claim in self.by_field.get(name, ()):
                if claim.value not in result:
                    result.append(claim.value)
        return tuple(result)


@dataclass(slots=True)
class LLMGuardrails:
    """Validate proposed claims/decisions against existing evidence."""

    autopilot_allowed_fields: frozenset[str] = AUTOPILOT_DEFAULT_FIELDS
    id_fields: frozenset[str] = ID_FIELDS
    max_cost_usd_per_item: float | None = None
    _last_rejections: list[GuardrailRejection] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.autopilot_allowed_fields = frozenset(
            canonical_field(field) for field in self.autopilot_allowed_fields
        )
        if self.max_cost_usd_per_item is not None and self.max_cost_usd_per_item < 0:
            raise ValueError("max_cost_usd_per_item must be non-negative")

    @property
    def last_rejections(self) -> tuple[GuardrailRejection, ...]:
        return tuple(self._last_rejections)

    def check_response(
        self,
        response: LLMResponse,
        *,
        claims: Sequence[Claim],
        target_fields: Sequence[str] = (),
        mode: DecisionMode = DecisionMode.LLM_ASSISTED,
        locked_fields: Sequence[str] = (),
        budget_usd: float | None = None,
    ) -> GuardrailResult:
        """Accept only proposals that are grounded in ``claims``."""

        budget = self.max_cost_usd_per_item if budget_usd is None else budget_usd
        if budget is not None and response.cost_usd is not None:
            if response.cost_usd > budget:
                raise BudgetExceeded(
                    "LLM cost exceeds the per-item budget",
                    context={
                        "cost_usd": response.cost_usd,
                        "max_cost_usd_per_item": budget,
                        "model": response.model,
                    },
                )

        index = _ClaimIndex.build(claims)
        allowed = {canonical_field(field) for field in target_fields}
        locked = {canonical_field(field) for field in locked_fields}
        rejections: list[GuardrailRejection] = []
        accepted_claims = [
            claim
            for claim in response.claims
            if self._check_claim(claim, index, allowed, rejections)
        ]
        accepted_decisions: list[LLMDecision] = []
        seen: dict[str, LLMDecision] = {}
        for decision in response.decisions:
            if not self._check_decision(
                decision,
                index,
                allowed,
                locked,
                mode,
                rejections,
            ):
                continue
            canonical = _canonical_llm_field(decision.field)
            if canonical in seen:
                rejections.append(
                    GuardrailRejection(
                        field=canonical,
                        code="contradictory_duplicate",
                        message="the response contains more than one decision for this field",
                        value=decision.value,
                    )
                )
                seen.pop(canonical, None)
                continue
            seen[canonical] = decision
            accepted_decisions.append(decision)
        # If a duplicate was found, all decisions for that field are unsafe.
        conflicted = {
            rejection.field
            for rejection in rejections
            if rejection.code == "contradictory_duplicate"
        }
        accepted_decisions = [
            decision
            for decision in accepted_decisions
            if _canonical_llm_field(decision.field) not in conflicted
        ]
        warnings: list[str] = []
        if response.cost_usd is None:
            warnings.append("llm cost is unknown: configure price_in/price_out to record USD cost")
        if response.unknown:
            unknown = sorted(set(response.unknown))
            preview = ", ".join(unknown[:8])
            suffix = "" if len(unknown) <= 8 else f" (+{len(unknown) - 8})"
            warnings.append(f"llm reported {len(unknown)} unknown field(s): {preview}{suffix}")
        result = GuardrailResult(
            claims=tuple(accepted_claims),
            decisions=tuple(accepted_decisions),
            rejections=tuple(rejections),
            warnings=tuple(warnings),
        )
        self._last_rejections = list(result.rejections)
        return result

    # Compatibility aliases for callers that name the operation "validate".
    check = check_response
    validate = check_response
    validate_response = check_response

    def _check_claim(
        self,
        claim: LLMClaim,
        index: _ClaimIndex,
        allowed: set[str],
        rejections: list[GuardrailRejection],
    ) -> bool:
        canonical = _canonical_llm_field(claim.field)
        if allowed and canonical not in allowed:
            rejections.append(
                GuardrailRejection(
                    field=canonical,
                    code="field_not_allowed",
                    message="field is not a target field for this task",
                    value=claim.value,
                )
            )
            return False
        if claim.value is None:
            rejections.append(
                GuardrailRejection(
                    field=canonical,
                    code="null_claim",
                    message="use the unknown list for fields without evidence",
                    value=claim.value,
                )
            )
            return False
        if not claim.evidence_claim_ids and not (claim.quote or "").strip():
            rejections.append(
                GuardrailRejection(
                    field=canonical,
                    code="missing_evidence",
                    message="LLM claims need an evidence claim id or a source quote",
                    value=claim.value,
                )
            )
            return False
        if not self._evidence_ids_exist(claim.evidence_claim_ids, index, canonical, rejections):
            return False
        if not self._grounded_value(canonical, claim.value, index, rejections):
            return False
        validation = validate_field(canonical, claim.value)
        if not validation.valid:
            rejections.append(
                GuardrailRejection(
                    field=canonical,
                    code="invalid_value",
                    message="; ".join(validation.errors),
                    value=claim.value,
                )
            )
            return False
        return True

    def _check_decision(
        self,
        decision: LLMDecision,
        index: _ClaimIndex,
        allowed: set[str],
        locked: set[str],
        mode: DecisionMode,
        rejections: list[GuardrailRejection],
    ) -> bool:
        canonical = _canonical_llm_field(decision.field)
        if allowed and canonical not in allowed:
            rejections.append(
                GuardrailRejection(
                    field=canonical,
                    code="field_not_allowed",
                    message="field is not a target field for this task",
                    value=decision.value,
                )
            )
            return False
        if canonical in locked:
            rejections.append(
                GuardrailRejection(
                    field=canonical,
                    code="locked_field",
                    message="user lock wins over the LLM decision",
                    value=decision.value,
                )
            )
            return False
        if mode is DecisionMode.LLM_AUTOPILOT:
            if canonical in self.id_fields:
                rejections.append(
                    GuardrailRejection(
                        field=canonical,
                        code="autopilot_id_forbidden",
                        message="external IDs are policy-only in autopilot",
                        value=decision.value,
                    )
                )
                return False
            if canonical not in self.autopilot_allowed_fields:
                rejections.append(
                    GuardrailRejection(
                        field=canonical,
                        code="autopilot_field_forbidden",
                        message="field is outside the autopilot allowlist",
                        value=decision.value,
                    )
                )
                return False
        if not self._evidence_ids_exist(
            (*decision.chosen_claim_ids, *decision.derived_from),
            index,
            canonical,
            rejections,
        ):
            return False
        if decision.value is None:
            if decision.chosen_claim_ids or decision.derived_from:
                rejections.append(
                    GuardrailRejection(
                        field=canonical,
                        code="unknown_with_evidence",
                        message="an unknown decision must not claim supporting evidence",
                    )
                )
                return False
            return True
        if decision.value is not None:
            if not decision.chosen_claim_ids and not decision.derived_from:
                rejections.append(
                    GuardrailRejection(
                        field=canonical,
                        code="missing_evidence",
                        message="a decision value needs chosen claims or derived_from ids",
                        value=decision.value,
                    )
                )
                return False
            if not self._grounded_value(canonical, decision.value, index, rejections):
                return False
            validation = validate_field(canonical, decision.value)
            if not validation.valid:
                rejections.append(
                    GuardrailRejection(
                        field=canonical,
                        code="invalid_value",
                        message="; ".join(validation.errors),
                        value=decision.value,
                    )
                )
                return False
            if (
                decision.chosen_claim_ids
                and not decision.derived_from
                and not self._value_matches_chosen(decision, canonical, index)
            ):
                rejections.append(
                    GuardrailRejection(
                        field=canonical,
                        code="synthesis_without_derived_from",
                        message="a synthesized value requires derived_from claim ids",
                        value=decision.value,
                    )
                )
                return False
        return True

    def _value_matches_chosen(
        self,
        decision: LLMDecision,
        canonical: str,
        index: _ClaimIndex,
    ) -> bool:
        for claim_id in decision.chosen_claim_ids:
            claim = index.by_id.get(claim_id)
            if claim is not None and value_equal(canonical, claim.value, decision.value):
                return True
        return False

    def _evidence_ids_exist(
        self,
        ids: Sequence[str],
        index: _ClaimIndex,
        field: str,
        rejections: list[GuardrailRejection],
    ) -> bool:
        missing = [claim_id for claim_id in ids if claim_id not in index.by_id]
        if missing:
            rejections.append(
                GuardrailRejection(
                    field=field,
                    code="unknown_claim_id",
                    message="evidence references claim ids that are not in the input",
                    value=missing[0],
                )
            )
            return False
        return True

    def _grounded_value(
        self,
        canonical: str,
        value: FieldValue,
        index: _ClaimIndex,
        rejections: list[GuardrailRejection],
    ) -> bool:
        if canonical in self.id_fields:
            existing_values = index.values(canonical)
            if not any(value_equal(canonical, value, existing) for existing in existing_values):
                rejections.append(
                    GuardrailRejection(
                        field=canonical,
                        code="invented_id",
                        message="external IDs must be selected from existing claims",
                        value=value,
                    )
                )
                return False
            return True
        if isinstance(value, list):
            return all(self._grounded_value(canonical, item, index, rejections) for item in value)
        if not isinstance(value, str):
            return True
        if _looks_like_invented_reference(value):
            if not _matches_any_claim_value(value, index):
                rejections.append(
                    GuardrailRejection(
                        field=canonical,
                        code="invented_reference",
                        message="MBID/ISRC/URL values must already exist in the claims table",
                        value=value,
                    )
                )
                return False
        return True


def _canonical_llm_field(field: str) -> str:
    normalized = field.strip().lower()
    if normalized in _ID_FIELD_ALIASES:
        return _ID_FIELD_ALIASES[normalized]
    if normalized in _SUBJECT_FIELD_ALIASES:
        return _SUBJECT_FIELD_ALIASES[normalized]
    if "." in normalized:
        prefix, _, name = normalized.partition(".")
        if prefix in {"item", "recording", "release", "artist", "derivative"}:
            return canonical_field(name)
    return canonical_field(normalized)


def _looks_like_invented_reference(value: str) -> bool:
    text = value.strip()
    if _URL_RE.match(text) or _ISRC_RE.match(text):
        return True
    try:
        uuid.UUID(text)
    except (ValueError, AttributeError):
        return False
    return bool(_MBID_RE.match(text))


def _matches_any_claim_value(value: str, index: _ClaimIndex) -> bool:
    for claims in index.by_field.values():
        for claim in claims:
            if isinstance(claim.value, str) and claim.value.strip() == value.strip():
                return True
    return False


def validate_payload(payload: object, schema: Mapping[str, Any]) -> None:
    """Validate the JSON-schema subset used by the strict output contract.

    Providers are expected to enforce this themselves; the check is repeated
    here so a JSON-mode fallback or a fake provider cannot bypass it.
    """

    _validate_node(payload, schema, path="$")


def _validate_node(value: object, schema: Mapping[str, Any], *, path: str) -> None:
    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, (list, tuple)) else [expected]
        if not any(_type_matches(value, item) for item in types):
            raise LLMInvalidOutput(
                f"LLM output at {path} has the wrong type",
                context={"path": path, "expected": expected, "actual": type(value).__name__},
            )
    enum_values = schema.get("enum")
    if isinstance(enum_values, (list, tuple)) and value not in enum_values:
        raise LLMInvalidOutput(
            f"LLM output at {path} is not an allowed enum value",
            context={"path": path},
        )
    if isinstance(value, Mapping):
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping):
            properties = {}
        required = schema.get("required", [])
        if isinstance(required, (list, tuple)):
            missing = [name for name in required if name not in value]
            if missing:
                raise LLMInvalidOutput(
                    f"LLM output at {path} is missing required fields",
                    context={"path": path, "missing": missing},
                )
        if schema.get("additionalProperties") is False:
            unknown = sorted(set(value) - set(properties))
            if unknown:
                raise LLMInvalidOutput(
                    f"LLM output at {path} contains unknown fields",
                    context={"path": path, "unknown": unknown},
                )
        for name, child in value.items():
            child_schema = properties.get(name)
            if isinstance(child_schema, Mapping):
                _validate_node(child, child_schema, path=f"{path}.{name}")
    elif isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, Mapping):
            for index, item in enumerate(value):
                _validate_node(item, items, path=f"{path}[{index}]")


def _type_matches(value: object, expected: object) -> bool:
    if expected == "null":
        return value is None
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return False


def canonical_llm_field(field: str) -> str:
    """Map subject-qualified LLM field names to canonical policy fields."""

    return _canonical_llm_field(field)


def validate_llm_output(payload: object, schema: Mapping[str, Any]) -> None:
    """Compatibility alias for :func:`validate_payload`."""

    validate_payload(payload, schema)


__all__ = [
    "AUTOPILOT_DEFAULT_FIELDS",
    "ID_FIELDS",
    "GuardrailRejection",
    "GuardrailResult",
    "LLMGuardrails",
    "canonical_llm_field",
    "validate_llm_output",
    "validate_payload",
]
