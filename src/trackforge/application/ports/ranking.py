"""Candidate ranking ports.

Rankers order an existing finite candidate set.  They never generate text, and
their scores are evidence for policy decisions rather than calibrated
probabilities.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model._validation import ensure_float, ensure_int, require_text
from trackforge.domain.model.claims import Claim


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    """One ranked claim and the evidence behind its score."""

    claim: Claim
    score: float
    ranker: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.claim, Claim):
            raise DomainValidationError("candidate must be a Claim")
        object.__setattr__(
            self,
            "score",
            ensure_float(self.score, "score", minimum=0.0, maximum=1.0),
        )
        object.__setattr__(self, "ranker", require_text(self.ranker, "ranker"))

    @property
    def claim_id(self) -> str:
        return str(self.claim.id)


@dataclass(frozen=True, slots=True)
class RankerStatus:
    """Availability and degradation details for one ranker layer."""

    id: str
    available: bool
    strategy: str
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", require_text(self.id, "id"))
        object.__setattr__(self, "strategy", require_text(self.strategy, "strategy"))
        object.__setattr__(self, "warnings", tuple(self.warnings))


@runtime_checkable
class ClaimRanker(Protocol):
    """Rank an existing set of claims for a free-text query."""

    def rank(
        self,
        query: str,
        candidates: Sequence[Claim],
        limit: int = 10,
    ) -> list[ScoredCandidate]: ...


def validate_limit(limit: int) -> int:
    return ensure_int(limit, "limit", minimum=1)


__all__ = [
    "ClaimRanker",
    "RankerStatus",
    "ScoredCandidate",
    "validate_limit",
]
