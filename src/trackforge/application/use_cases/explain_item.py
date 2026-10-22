"""Explain one persisted field decision with its evidence and alternatives."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from trackforge.application.dto.decisions import (
    AlternativeView,
    ClaimView,
    ExplanationView,
)
from trackforge.application.ports.repositories import UnitOfWork
from trackforge.domain.errors import (
    DomainValidationError,
    RecordNotFound,
    ResolutionFailed,
    UnsupportedMode,
)
from trackforge.domain.model.claims import Claim, Subject
from trackforge.domain.model.decisions import Decision, DecisionMode, DecisionRecord
from trackforge.domain.model.ids import DecisionRecordId, ItemId
from trackforge.domain.policies.normalization import canonical_field, value_equal
from trackforge.domain.policies.validators import validate_cross_fields, validate_field

_ID_FIELDS = frozenset(
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


class ExplainItem:
    """Read a decision record and build a presentation-safe explanation."""

    def __init__(self, unit_of_work_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = unit_of_work_factory

    async def explain(
        self,
        item_id: ItemId,
        field: str,
        *,
        record_id: DecisionRecordId | str | None = None,
        subject: Subject | str = Subject.ITEM,
    ) -> ExplanationView:
        """Explain the current (or explicitly selected) decision for one field."""

        target_subject = _coerce_subject(subject)
        field_name = canonical_field(field)
        async with self._uow_factory() as uow:
            item = await uow.items.get(item_id)
            if item is None:
                raise RecordNotFound("item not found", context={"item_id": item_id})
            record = await _load_record(uow, item.id, item.decision_record_id, record_id)
            decision = _find_decision(record, field_name, target_subject)
            if decision is None:
                raise RecordNotFound(
                    "no decision for field",
                    context={
                        "item_id": item_id,
                        "field": field_name,
                        "record_id": record.id,
                    },
                )
            if decision.mode is not DecisionMode.POLICY:
                raise UnsupportedMode(
                    f"decision mode {decision.mode.value!r} cannot be explained by policy",
                    context={"field": field_name, "mode": decision.mode.value},
                )
            claims = await uow.claims.list_by_item(item.id)

        by_id = {claim.id: claim for claim in claims}
        chosen = by_id.get(decision.chosen_claim_id) if decision.chosen_claim_id else None
        derived = tuple(
            ClaimView.from_claim(by_id[claim_id])
            for claim_id in decision.derived_from
            if claim_id in by_id
        )
        alternatives = tuple(
            AlternativeView(
                claim=ClaimView.from_claim(by_id[claim_id]),
                reason=_alternative_reason(by_id[claim_id], decision, chosen),
            )
            for claim_id in decision.alternatives
            if claim_id in by_id and claim_id != decision.chosen_claim_id
        )
        warnings = _warnings(decision, claims, record)
        return ExplanationView(
            item_id=item_id,
            field=decision.field,
            decision=decision.value,
            mode=decision.mode,
            policy_id=decision.policy_id,
            policy_version=_policy_version(record, decision.policy_id),
            confidence=decision.confidence,
            rationale=decision.rationale,
            chosen_claim=ClaimView.from_claim(chosen) if chosen is not None else None,
            derived_from=derived,
            alternatives=alternatives,
            warnings=warnings,
            locked=decision.locked,
            decision_record_id=record.id,
            record_version=record.version,
        )


async def _load_record(
    uow: UnitOfWork,
    item_id: ItemId,
    current_record_id: DecisionRecordId | None,
    record_id: DecisionRecordId | str | None,
) -> DecisionRecord:
    if record_id is not None:
        target = DecisionRecordId(str(record_id))
        record = await uow.decisions.get_record(target)
        if record is None or record.item_id != item_id:
            raise RecordNotFound(
                "decision record not found for item",
                context={"item_id": item_id, "record_id": str(record_id)},
            )
        return record
    if current_record_id is not None:
        record = await uow.decisions.get_record(current_record_id)
        if record is not None:
            return record
    records = await uow.decisions.list_records(item_id)
    if not records:
        raise ResolutionFailed(
            "item has no decision record",
            user_action="Resolve the item before asking for an explanation.",
            context={"item_id": item_id},
        )
    return records[-1]


def _find_decision(
    record: DecisionRecord,
    field: str,
    subject: Subject,
) -> Decision | None:
    decision = record.decision_for(field, subject=subject)
    if decision is not None:
        return decision
    # Accept the alternate spelling for the artwork/cover policy pair.
    if field == "artwork":
        return record.decision_for("cover", subject=subject)
    if field == "cover":
        return record.decision_for("artwork", subject=subject)
    return None


def _alternative_reason(
    claim: Claim,
    decision: Decision,
    chosen: Claim | None,
) -> str:
    validation = validate_field(claim.field, claim.value)
    if not validation.valid:
        return "invalid value: " + "; ".join(validation.errors)
    if decision.value is not None and value_equal(
        decision.field,
        claim.value,
        decision.value,
    ):
        return "duplicate normalized value"
    if claim.source == "path" and chosen is not None and chosen.source != "path":
        return "lower-priority path evidence"
    if chosen is not None and float(claim.confidence) < float(chosen.confidence):
        return f"lower confidence ({float(claim.confidence):.2f})"
    if decision.derived_from:
        return "superseded by a derived value"
    if claim.source == "initial_tags":
        return "lower source priority"
    return "rejected by the active policy"


def _warnings(
    decision: Decision,
    claims: Sequence[Claim],
    record: DecisionRecord,
) -> tuple[str, ...]:
    warnings: list[str] = []
    if decision.value is not None:
        result = validate_field(decision.field, decision.value)
        warnings.extend(f"{decision.field}: {item}" for item in result.warnings)
    if decision.value is None:
        warnings.append("unknown is a valid decision; no value was invented")
    source_noise = {
        str(claim.value)
        for claim in claims
        if claim.field == "source_noise" and isinstance(claim.value, str)
    }
    warnings.extend(f"source_noise:{marker}" for marker in sorted(source_noise))
    if decision.field in _ID_FIELDS and decision.value is None:
        warnings.append("external IDs require authority evidence; policy mode leaves them unknown")
    values = {item.field: item.value for item in record.decisions if item.subject is Subject.ITEM}
    cross = validate_cross_fields(values)
    warnings.extend(item.message for item in cross.issues)
    return tuple(dict.fromkeys(warnings))


def _policy_version(record: DecisionRecord, policy_id: str) -> int:
    for value in record.policy_versions:
        identifier, separator, version = value.rpartition("@")
        if not separator:
            continue
        if identifier == policy_id:
            try:
                return int(version)
            except ValueError:
                return 1
    return 1


def _coerce_subject(value: Subject | str) -> Subject:
    if isinstance(value, Subject):
        return value
    try:
        return Subject(value)
    except ValueError as exc:
        raise DomainValidationError(
            "subject must be one of the Subject values",
            context={"subject": value},
        ) from exc


__all__ = ["ExplainItem"]
