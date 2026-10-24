"""Pure field-selection strategies.

A strategy receives already validated claims and returns a selection.  It
never writes to the catalog and never changes claim values.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from trackforge.domain.model._validation import FieldValue
from trackforge.domain.model.claims import Claim
from trackforge.domain.model.decisions import PolicyStrategy
from trackforge.domain.model.identity import Transformation
from trackforge.domain.model.ids import ClaimId
from trackforge.domain.policies.normalization import (
    MULTI_VALUE_FIELDS,
    apply_qualifier,
    canonical_field,
    comparison_key,
    display_qualifier,
    normalize_artist_name,
    normalize_bpm,
    normalize_date,
    normalize_key,
    rank_transformations,
    strip_qualifier,
    title_has_qualifier,
)
from trackforge.domain.policies.validators import ValidationResult, validate_field


@dataclass(frozen=True, slots=True)
class StrategyResult:
    """The outcome of applying one strategy to one field."""

    field: str
    value: FieldValue = None
    selected: tuple[Claim, ...] = ()
    alternatives: tuple[Claim, ...] = ()
    confidence: float = 0.0
    rationale: str = ""
    strategy: PolicyStrategy = PolicyStrategy.SOURCE_PRIORITY
    derived_from: tuple[ClaimId, ...] = ()
    synthesized: bool = False
    review_required: bool = False
    locked: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "field", canonical_field(self.field))
        object.__setattr__(self, "selected", tuple(self.selected))
        object.__setattr__(self, "alternatives", tuple(self.alternatives))
        object.__setattr__(self, "derived_from", tuple(self.derived_from))
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("strategy confidence must be between 0 and 1")
        if not self.rationale:
            raise ValueError("strategy rationale is required")
        if (
            self.value is not None
            and not self.selected
            and not self.derived_from
            and not self.locked
        ):
            raise ValueError("a non-unknown strategy result needs a claim or derivation")

    @property
    def chosen_claim_id(self) -> ClaimId | None:
        if self.synthesized or len(self.selected) != 1:
            return None
        return self.selected[0].id

    @property
    def chosen_claim_ids(self) -> tuple[ClaimId, ...]:
        return tuple(claim.id for claim in self.selected)

    @property
    def is_unknown(self) -> bool:
        return self.value is None


def select_source_priority(
    field: str,
    claims: Sequence[Claim],
    priority: Sequence[str] = (),
    *,
    multi: bool | None = None,
) -> StrategyResult:
    """Select the first valid value from a configured source order.

    Sources not listed in ``priority`` are a conservative fallback: when no
    configured token matches, the highest-confidence claim wins.  This keeps
    test fixtures, imported claims, and future providers usable without
    silently discarding evidence.
    """

    canonical = canonical_field(field)
    candidates = _eligible(canonical, claims)
    if not candidates:
        return unknown_result(canonical, claims, reason="no validated claims for field")

    selected: tuple[Claim, ...] = ()
    used_token: str | None = None
    for token in priority:
        if token in {"unknown", "user_lock"}:
            continue
        matched = tuple(claim for claim in candidates if _matches_priority(claim, token, canonical))
        if matched:
            selected = matched
            used_token = token
            break

    fallback = not selected
    if fallback:
        selected = candidates

    selection = _select_from_bucket(
        canonical,
        selected,
        all_claims=claims,
        multi=multi,
    )
    if fallback:
        rationale = (
            f"selected the highest-confidence claim for {canonical} "
            f"({selection.selected[0].source}, {selection.confidence:.2f})"
            if selection.selected
            else f"no claim matched the configured priority for {canonical}"
        )
    else:
        rationale = (
            f"source priority selected {used_token} for {canonical}; "
            f"{len(selection.alternatives)} alternative(s) rejected"
        )
    return StrategyResult(
        field=canonical,
        value=selection.value,
        selected=selection.selected,
        alternatives=selection.alternatives,
        confidence=selection.confidence,
        rationale=rationale,
        strategy=PolicyStrategy.SOURCE_PRIORITY,
        derived_from=selection.derived_from,
        synthesized=selection.synthesized,
    )


# Short alias used in tests and call sites.
source_priority = select_source_priority


def select_keep_existing(
    field: str,
    claims: Sequence[Claim],
    priority: Sequence[str] = (),
) -> StrategyResult:
    """Prefer a valid existing tag, then fall back to the configured order."""

    canonical = canonical_field(field)
    existing = tuple(
        claim for claim in _eligible(canonical, claims) if claim.source == "initial_tags"
    )
    if existing:
        selection = _select_from_bucket(
            canonical,
            existing,
            all_claims=claims,
            multi=canonical in MULTI_VALUE_FIELDS,
        )
        return StrategyResult(
            field=canonical,
            value=selection.value,
            selected=selection.selected,
            alternatives=selection.alternatives,
            confidence=selection.confidence,
            rationale=(
                f"kept existing {canonical} tag; "
                f"{len(selection.alternatives)} alternative(s) rejected"
            ),
            strategy=PolicyStrategy.KEEP_EXISTING,
            derived_from=selection.derived_from,
            synthesized=selection.synthesized,
        )
    result = select_source_priority(canonical, claims, priority)
    return StrategyResult(
        field=canonical,
        value=result.value,
        selected=result.selected,
        alternatives=result.alternatives,
        confidence=result.confidence,
        rationale=f"no existing {canonical} tag; {result.rationale}",
        strategy=PolicyStrategy.KEEP_EXISTING,
        derived_from=result.derived_from,
        synthesized=result.synthesized,
    )


keep_existing = select_keep_existing


def select_consensus(
    field: str,
    claims: Sequence[Claim],
    *,
    boost: float = 0.1,
    maximum: float = 0.99,
) -> StrategyResult:
    """Choose the strongest group of claims that agree after normalization.

    Only independent claims (different source, no derivation relationship)
    contribute to the agreement boost.  Duplicate claims from one source do
    not inflate confidence.
    """

    canonical = canonical_field(field)
    candidates = _eligible(canonical, claims)
    if not candidates:
        return unknown_result(canonical, claims, reason="no validated claims for consensus")

    groups = _group_claims(canonical, candidates)
    ranked: list[tuple[float, float, tuple[Claim, ...]]] = []
    for group in groups.values():
        representative = max(group, key=lambda claim: (float(claim.confidence), claim.id))
        independent = _independent_count(group)
        confidence = min(maximum, float(representative.confidence) + boost * (independent - 1))
        ranked.append((confidence, float(representative.confidence), group))
    ranked.sort(key=lambda item: (item[0], item[1], item[2][0].id), reverse=True)
    confidence, _, winners = ranked[0]
    winner = max(winners, key=lambda claim: (float(claim.confidence), claim.id))
    alternatives = tuple(claim for claim in claims if claim.id != winner.id)
    independent = _independent_count(winners)
    rationale = (
        f"consensus of {independent} independent source(s) for {canonical}; agreement boost applied"
        if independent > 1
        else f"consensus fallback to strongest single-source claim for {canonical}"
    )
    return StrategyResult(
        field=canonical,
        value=winner.value,
        selected=(winner,),
        alternatives=alternatives,
        confidence=confidence,
        rationale=rationale,
        strategy=PolicyStrategy.CONSENSUS,
    )


consensus = select_consensus


def select_derive(
    field: str,
    base: StrategyResult,
    transformations: Iterable[Claim | Transformation | str],
    *,
    qualifier: str | None = None,
) -> StrategyResult:
    """Derive a title (or other string field) from a base value + qualifier."""

    canonical = canonical_field(field)
    if base.value is None or not isinstance(base.value, str):
        return StrategyResult(
            field=canonical,
            value=base.value,
            selected=base.selected,
            alternatives=base.alternatives,
            confidence=base.confidence,
            rationale=f"cannot derive {canonical}: base value is unknown",
            strategy=PolicyStrategy.DERIVE,
            review_required=base.review_required,
        )

    transformation_claims: list[Claim] = []
    raw_transformations: list[Transformation] = []
    for item in transformations:
        if isinstance(item, Claim):
            transformation_claims.append(item)
            raw_values = item.value if isinstance(item.value, list) else [item.value]
            for raw_value in raw_values:
                try:
                    raw_transformations.append(Transformation(str(raw_value)))
                except ValueError:
                    continue
        elif isinstance(item, Transformation):
            raw_transformations.append(item)
        elif isinstance(item, str):
            try:
                raw_transformations.append(Transformation(item))
            except ValueError:
                continue

    ranked = rank_transformations(raw_transformations)
    if not ranked:
        return StrategyResult(
            field=canonical,
            value=base.value,
            selected=base.selected,
            alternatives=base.alternatives,
            confidence=base.confidence,
            rationale=f"no derivative qualifier for {canonical}; kept base value",
            strategy=PolicyStrategy.DERIVE,
            derived_from=base.derived_from,
            synthesized=base.synthesized,
            locked=base.locked,
        )

    primary = ranked[0]
    label = qualifier or display_qualifier(primary)
    derived_value = base.value
    if title_has_qualifier(base.value, primary):
        cleaned = strip_qualifier(base.value, primary)
        normalized = (
            apply_qualifier(cleaned, primary)
            if cleaned and cleaned != base.value and not title_has_qualifier(cleaned, primary)
            else base.value
        )
        if normalized != base.value:
            derived_value = normalized
        else:
            return StrategyResult(
                field=canonical,
                value=base.value,
                selected=base.selected,
                alternatives=base.alternatives,
                confidence=base.confidence,
                rationale=(f"base {canonical} already contains qualifier {label}; not duplicated"),
                strategy=PolicyStrategy.DERIVE,
                derived_from=base.derived_from,
                synthesized=base.synthesized,
                locked=base.locked,
            )
    else:
        derived_value = apply_qualifier(base.value, primary)
    transformation_ids = tuple(
        claim.id for claim in transformation_claims if _claim_has_transformation(claim, primary)
    )
    base_ids = tuple(claim.id for claim in base.selected) if base.chosen_claim_id is None else ()
    derived_from = _unique_ids((*base_ids, *transformation_ids))
    confidence = min(0.99, max(base.confidence, _max_confidence(transformation_claims)))
    return StrategyResult(
        field=canonical,
        value=derived_value,
        selected=base.selected,
        alternatives=base.alternatives,
        confidence=confidence,
        rationale=(
            f"derived {canonical} from base value and {label} transformation; "
            "qualifier appended once"
        ),
        strategy=PolicyStrategy.DERIVE,
        derived_from=derived_from,
        synthesized=base.synthesized,
        locked=base.locked,
    )


derive = select_derive


def select_unknown(
    field: str,
    claims: Sequence[Claim] = (),
    *,
    reason: str = "no evidence above the policy threshold",
    review_required: bool = False,
) -> StrategyResult:
    """Return a legal ``unknown`` (or review-required) selection."""

    canonical = canonical_field(field)
    return StrategyResult(
        field=canonical,
        value=None,
        selected=(),
        alternatives=tuple(claims),
        confidence=0.0,
        rationale=reason,
        strategy=PolicyStrategy.REVIEW if review_required else PolicyStrategy.UNKNOWN,
        review_required=review_required,
    )


unknown_result = select_unknown


def select_review(
    field: str,
    claims: Sequence[Claim] = (),
    *,
    reason: str = "policy requires human review",
) -> StrategyResult:
    """Return an unknown value marked for review."""

    return select_unknown(field, claims, reason=reason, review_required=True)


review = select_review
unknown = select_unknown


def select_user_lock(
    field: str,
    value: FieldValue,
    claims: Sequence[Claim] = (),
    *,
    user_claim: Claim | None = None,
    validation: ValidationResult | None = None,
) -> StrategyResult:
    """Return a locked selection; the caller owns persistence of the lock."""

    canonical = canonical_field(field)
    if value is None:
        return StrategyResult(
            field=canonical,
            value=None,
            selected=(user_claim,) if user_claim is not None else (),
            alternatives=tuple(claim for claim in claims if claim is not user_claim),
            confidence=1.0,
            rationale="user lock explicitly clears the field",
            strategy=PolicyStrategy.USER_LOCK,
            locked=True,
        )
    selected = (user_claim,) if user_claim is not None else ()
    alternatives = tuple(claim for claim in claims if claim.id != getattr(user_claim, "id", None))
    warnings = () if validation is None else validation.errors
    rationale = "user lock supplies the value"
    if warnings:
        rationale = f"{rationale}; validation warnings: {'; '.join(warnings)}"
    return StrategyResult(
        field=canonical,
        value=value,
        selected=selected,
        alternatives=alternatives,
        confidence=1.0,
        rationale=rationale,
        strategy=PolicyStrategy.USER_LOCK,
        derived_from=() if user_claim is not None else tuple(claim.id for claim in claims),
        locked=True,
    )


user_lock = select_user_lock


def _eligible(field: str, claims: Sequence[Claim]) -> tuple[Claim, ...]:
    candidates: list[Claim] = []
    for claim in claims:
        if claim.value is None:
            continue
        if field == "artist" and claim.role not in {None, "main", "featured"}:
            continue
        if field == "albumartist" and claim.role not in {None, "albumartist", "main"}:
            continue
        validation = validate_field(field, claim.value)
        if validation.valid:
            candidates.append(claim)
    return tuple(candidates)


def _select_from_bucket(
    field: str,
    bucket: Sequence[Claim],
    *,
    all_claims: Sequence[Claim],
    multi: bool | None,
) -> StrategyResult:
    use_multi = field in MULTI_VALUE_FIELDS if multi is None else multi
    if not bucket:
        return unknown_result(field, all_claims)
    groups = _group_claims(field, bucket)
    representatives: list[Claim] = []
    for group in groups.values():
        representatives.append(max(group, key=lambda claim: (float(claim.confidence), claim.id)))
    representatives.sort(key=lambda claim: _claim_sort_key(field, claim))

    if use_multi:
        selected = tuple(representatives)
        selected_ids = {claim.id for claim in selected}
        alternatives = tuple(claim for claim in all_claims if claim.id not in selected_ids)
        values = [claim.value for claim in selected if claim.value is not None]
        value: FieldValue
        if len(values) == 1:
            value = _coerce_value(values[0])
        else:
            value = [_string_value(item) for item in values]
        confidence = max(float(claim.confidence) for claim in selected)
        return StrategyResult(
            field=field,
            value=value,
            selected=selected,
            alternatives=alternatives,
            confidence=confidence,
            rationale=(
                f"selected {len(selected)} {field} claim(s) from the highest-priority source"
            ),
            derived_from=tuple(claim.id for claim in selected) if len(selected) > 1 else (),
            synthesized=len(selected) > 1,
        )

    winner = max(representatives, key=lambda claim: (float(claim.confidence), claim.id))
    alternatives = tuple(claim for claim in all_claims if claim.id != winner.id)
    return StrategyResult(
        field=field,
        value=_coerce_value(winner.value),
        selected=(winner,),
        alternatives=alternatives,
        confidence=float(winner.confidence),
        rationale=f"selected {field} claim from {winner.source}",
    )


def _group_claims(field: str, claims: Sequence[Claim]) -> dict[str, tuple[Claim, ...]]:
    groups: dict[str, list[Claim]] = {}
    for claim in claims:
        groups.setdefault(_normalized_key(field, claim.value), []).append(claim)
    return {key: tuple(value) for key, value in groups.items()}


def _normalized_key(field: str, value: object) -> str:
    normalized = _normalized_value(field, value)
    return repr(normalized)


def _normalized_value(field: str, value: object) -> Any:
    if isinstance(value, str):
        if field in {"date", "originaldate"}:
            return normalize_date(value) or comparison_key(value)
        if field == "key":
            return normalize_key(value) or comparison_key(value)
        if field == "bpm":
            return normalize_bpm(value)
        if field in {"artist", "albumartist"}:
            return comparison_key(normalize_artist_name(value))
        return comparison_key(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (list, tuple)):
        return tuple(comparison_key(item) if isinstance(item, str) else item for item in value)
    return value


def _matches_priority(claim: Claim, token: str, field: str) -> bool:
    text = token.strip()
    if not text:
        return False
    if text == "external":
        return claim.field in {
            "mb_recording_id",
            "mb_release_id",
            "mb_release_group_id",
            "mb_artist_id",
            "mb_albumartist_id",
            "isrc",
            "acoustid",
        }
    if "." in text:
        prefix, _, suffix = text.partition(".")
        suffix_field = canonical_field(suffix)
        if prefix == "item":
            if claim.source == "user":
                # A user correction is stronger evidence than any automatic
                # source even when the user has not asked for a permanent lock.
                return claim.field in {suffix_field, field}
            return (claim.field == suffix_field and claim.source != "path") or (
                claim.field == field and claim.source != "path"
            )
        if prefix == "path":
            return claim.source == "path" and claim.field == suffix_field
        if prefix == "source":
            return claim.source == suffix_field
    if text == "source_url":
        return claim.field == "source_url" or (
            claim.field == "source" and claim.source in {"source", "url", "source_metadata"}
        )
    if text == "source_id":
        return claim.field == "source_id" or (
            claim.field == "source" and claim.source in {"source", "url", "source_metadata"}
        )
    return claim.source == text or claim.method.split("/")[0] == text


def _claim_sort_key(field: str, claim: Claim) -> tuple[int, int, float, str]:
    role_rank = 0
    if field == "artist":
        role_rank = 0 if claim.role in {None, "main"} else 1
    return (
        role_rank,
        claim.order if claim.order is not None else 10_000,
        -float(claim.confidence),
        claim.id,
    )


def _independent_count(claims: Sequence[Claim]) -> int:
    sources = {claim.source for claim in claims}
    return max(1, len(sources))


def _claim_has_transformation(claim: Claim, transformation: Transformation) -> bool:
    values = claim.value if isinstance(claim.value, list) else [claim.value]
    return any(value == transformation.value for value in values)


def _unique_ids(values: Sequence[ClaimId]) -> tuple[ClaimId, ...]:
    seen: set[ClaimId] = set()
    result: list[ClaimId] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)


def _max_confidence(claims: Sequence[Claim]) -> float:
    return max((float(claim.confidence) for claim in claims), default=0.0)


def _coerce_value(value: object) -> FieldValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_string_value(item) for item in value]
    return str(value)


def _string_value(value: object) -> str:
    return value if isinstance(value, str) else str(value)


Selection = StrategyResult
StrategyOutcome = StrategyResult


__all__ = [
    "Selection",
    "StrategyOutcome",
    "StrategyResult",
    "consensus",
    "derive",
    "keep_existing",
    "review",
    "select_consensus",
    "select_derive",
    "select_keep_existing",
    "select_review",
    "select_source_priority",
    "select_unknown",
    "select_user_lock",
    "source_priority",
    "unknown",
    "unknown_result",
    "user_lock",
]
