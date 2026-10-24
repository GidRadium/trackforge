"""Read-only decision and explanation DTOs.

``ResolvedMetadata`` is the domain output type; it is re-exported here so use
cases and callers can import all decision-facing DTOs from one module without
duplicating the canonical mapping schema.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from trackforge.domain.model._validation import FieldValue
from trackforge.domain.model.claims import Claim, Subject
from trackforge.domain.model.decisions import DecisionMode
from trackforge.domain.model.ids import ClaimId, DecisionRecordId, ItemId
from trackforge.domain.model.item import ItemStatus
from trackforge.domain.model.outputs import ResolvedMetadata

__all__ = [
    "AlternativeView",
    "ClaimView",
    "ExplanationView",
    "InterpretationView",
    "ResolvedMetadata",
]


@dataclass(frozen=True, slots=True)
class ClaimView:
    """The explainability view of one claim used by a decision."""

    id: ClaimId
    subject: Subject
    field: str
    value: FieldValue
    source: str
    method: str
    confidence: float
    role: str | None = None
    order: int | None = None
    derived_from: tuple[ClaimId, ...] = ()
    evidence: tuple[str, ...] = ()
    notes: str | None = None

    @property
    def claim_id(self) -> ClaimId:
        """Alias for callers that name the identifier explicitly."""

        return self.id

    @classmethod
    def from_claim(cls, claim: Claim) -> ClaimView:
        """Build a view without copying mutable claim state."""

        return cls(
            id=claim.id,
            subject=claim.subject,
            field=claim.field,
            value=claim.value,
            source=claim.source,
            method=claim.method,
            confidence=float(claim.confidence),
            role=claim.role,
            order=claim.order,
            derived_from=claim.derived_from,
            evidence=claim.evidence,
            notes=claim.notes,
        )


@dataclass(frozen=True, slots=True)
class AlternativeView:
    """A rejected claim plus the reason it lost the policy selection."""

    claim: ClaimView
    reason: str

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("alternative reason must be non-empty")

    @property
    def claim_id(self) -> ClaimId:
        return self.claim.id

    @property
    def value(self) -> FieldValue:
        return self.claim.value

    @property
    def source(self) -> str:
        return self.claim.source

    @property
    def method(self) -> str:
        return self.claim.method

    @property
    def confidence(self) -> float:
        return self.claim.confidence

    @property
    def role(self) -> str | None:
        return self.claim.role


@dataclass(frozen=True, slots=True)
class ExplanationView:
    """Everything needed to explain one field decision."""

    item_id: ItemId
    field: str
    decision: FieldValue
    mode: DecisionMode
    policy_id: str
    policy_version: int
    confidence: float
    rationale: str
    chosen_claim: ClaimView | None = None
    derived_from: tuple[ClaimView, ...] = ()
    alternatives: tuple[AlternativeView, ...] = ()
    warnings: tuple[str, ...] = ()
    locked: bool = False
    decision_record_id: DecisionRecordId | None = None
    record_version: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "derived_from", tuple(self.derived_from))
        object.__setattr__(self, "alternatives", tuple(self.alternatives))
        object.__setattr__(self, "warnings", tuple(self.warnings))

    @property
    def value(self) -> FieldValue:
        """Alias for the resolved value."""

        return self.decision

    @property
    def unknown(self) -> bool:
        return self.decision is None

    @property
    def chosen(self) -> ClaimView | None:
        """Alias convenient for CLI presenters."""

        return self.chosen_claim

    @property
    def chosen_claim_id(self) -> ClaimId | None:
        return None if self.chosen_claim is None else self.chosen_claim.id

    @property
    def derived_from_ids(self) -> tuple[ClaimId, ...]:
        return tuple(claim.id for claim in self.derived_from)

    @property
    def alternative_ids(self) -> tuple[ClaimId, ...]:
        return tuple(item.claim_id for item in self.alternatives)


@dataclass(frozen=True, slots=True)
class InterpretationView:
    """Result of turning observations into claims and identity hypotheses."""

    item_id: ItemId
    status: ItemStatus
    claims: tuple[Claim, ...] = ()
    identity: Mapping[str, Any] | None = None
    derivative: Mapping[str, Any] | None = None
    warnings: tuple[str, ...] = ()
    artifact_payloads: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "claims", tuple(self.claims))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "artifact_payloads", tuple(self.artifact_payloads))

    @property
    def claim_ids(self) -> tuple[ClaimId, ...]:
        return tuple(claim.id for claim in self.claims)
