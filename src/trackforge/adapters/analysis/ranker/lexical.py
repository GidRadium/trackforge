"""Lexical claim ranker: normalization plus rapidfuzz scoring.

Always available and dependency-light.  Scores are ranking evidence in
``[0, 1]``; they are not calibrated probabilities.
"""

from __future__ import annotations

from collections.abc import Sequence

from rapidfuzz import fuzz

from trackforge.application.ports.ranking import (
    ScoredCandidate,
    validate_limit,
)
from trackforge.domain.model.claims import Claim
from trackforge.domain.policies.normalization import comparison_key, normalize_text


class LexicalRanker:
    """Rank claims by token-set and character similarity to a query."""

    id = "lexical/rapidfuzz-v1"

    def __init__(self, *, token_set_weight: float = 0.7) -> None:
        if not 0.0 <= token_set_weight <= 1.0:
            raise ValueError("token_set_weight must be between 0 and 1")
        self._token_set_weight = token_set_weight

    def rank(
        self,
        query: str,
        candidates: Sequence[Claim],
        limit: int = 10,
    ) -> list[ScoredCandidate]:
        limit = validate_limit(limit)
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        query_key = comparison_key(normalize_text(query))
        scored: list[ScoredCandidate] = []
        for claim in candidates:
            if not isinstance(claim, Claim):
                raise TypeError("candidates must be Claim instances")
            if claim.value is None:
                continue
            text = _claim_text(claim)
            if not text:
                continue
            candidate_key = comparison_key(normalize_text(text))
            token_set = float(fuzz.token_set_ratio(query_key, candidate_key)) / 100.0
            ratio = float(fuzz.ratio(query_key, candidate_key)) / 100.0
            score = self._token_set_weight * token_set + (1.0 - self._token_set_weight) * ratio
            if query_key and query_key == candidate_key:
                score = 1.0
            elif query_key and (query_key in candidate_key or candidate_key in query_key):
                score = max(score, 0.98)
            scored.append(
                ScoredCandidate(
                    claim=claim,
                    score=round(min(1.0, max(0.0, score)), 6),
                    ranker=self.id,
                    evidence={
                        "algorithm": "rapidfuzz.token_set_ratio+ratio",
                        "token_set_ratio": round(token_set, 6),
                        "ratio": round(ratio, 6),
                        "normalized_query": query_key,
                        "normalized_candidate": candidate_key,
                        "score_kind": "similarity",
                        "calibrated": False,
                    },
                )
            )
        scored.sort(
            key=lambda item: (item.score, float(item.claim.confidence), str(item.claim.id)),
            reverse=True,
        )
        return scored[:limit]


def _claim_text(claim: Claim) -> str:
    value = claim.value
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(item for item in value if isinstance(item, str))
    return str(value)


__all__ = ["LexicalRanker"]
