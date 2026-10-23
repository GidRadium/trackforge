"""Review actions over resolved decisions, locks, and feedback events.

Review is a thin application service: it validates user input, appends a user
claim and/or lock, records a feedback event, then asks the resolver for a new
decision record.  Rendering and publication stay explicit caller actions so a
correction never mutates the library silently.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from trackforge.application.dto.decisions import AlternativeView, ClaimView
from trackforge.application.dto.views import (
    ReviewFieldView,
    ReviewView,
)
from trackforge.application.ports.repositories import UnitOfWork
from trackforge.application.use_cases.explain_item import ExplainItem
from trackforge.application.use_cases.resolve_item import ResolveItem
from trackforge.domain.errors import (
    DomainValidationError,
    RecordNotFound,
    ResolutionFailed,
)
from trackforge.domain.model._validation import FieldValue, utc_now
from trackforge.domain.model.claims import Claim, Confidence, Subject
from trackforge.domain.model.decisions import Decision, DecisionRecord
from trackforge.domain.model.feedback import (
    CorrectionScope,
    FeedbackEvent,
    FeedbackKind,
)
from trackforge.domain.model.ids import (
    ClaimId,
    FeedbackEventId,
    ItemId,
    new_uuid7,
)
from trackforge.domain.policies.normalization import canonical_field
from trackforge.domain.policies.validators import validate_field

_REVIEW_ACTIONS = frozenset({"show", "accept", "edit", "lock", "unlock", "unknown", "why", "skip"})


class ReviewItem:
    """Apply one review action and return the current review table."""

    def __init__(
        self,
        unit_of_work_factory: Callable[[], UnitOfWork],
        resolve_item: ResolveItem,
        explain_item: ExplainItem,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._uow_factory = unit_of_work_factory
        self._resolve_item = resolve_item
        self._explain_item = explain_item
        self._clock = clock

    async def review(
        self,
        item_id: ItemId,
        *,
        action: str = "show",
        field: str | None = None,
        value: Any = None,
        lock: bool = False,
        reason: str | None = None,
        mode: str = "policy",
        auto: bool = False,
    ) -> ReviewView:
        """Run a review action; ``show`` is side-effect free."""

        target_action = "accept" if auto and action == "show" else action
        if not isinstance(target_action, str) or target_action not in _REVIEW_ACTIONS:
            raise DomainValidationError(
                "unknown review action",
                context={"action": str(target_action), "known": sorted(_REVIEW_ACTIONS)},
            )
        field_name = canonical_field(field) if isinstance(field, str) and field.strip() else None
        if target_action in {"edit", "lock", "unlock", "unknown", "why"} and field_name is None:
            raise DomainValidationError(
                f"review action {target_action!r} requires a field",
                user_action="Pass --field <name>.",
                context={"action": target_action},
            )
        if target_action == "edit" and value is None:
            raise DomainValidationError(
                "review edit requires a value",
                user_action="Pass --set field=value or --field <name> --set <value>.",
                context={"field": field_name},
            )

        async with self._uow_factory() as uow:
            item = await uow.items.get(item_id)
            if item is None:
                raise RecordNotFound("item not found", context={"item_id": item_id})
            record = await _current_record(uow, item.id, item.decision_record_id)
            decision = _decision_for(record, field_name) if field_name else None
            claims = await uow.claims.list_by_item(item.id)
            if target_action == "why":
                required_field = _require_field(field_name)
                explanation = await self._explain_item.explain(item.id, required_field)
                view = _review_view(item, record, claims, record.id)
                return ReviewView(
                    item_id=view.item_id,
                    status=view.status,
                    fields=view.fields,
                    locked_fields=view.locked_fields,
                    warnings=view.warnings,
                    decision_record_id=view.decision_record_id,
                    explanation=explanation,
                    action="why",
                )
            if target_action == "show" or target_action == "skip":
                return _review_view(item, record, claims, record.id)

            if target_action == "accept":
                await self._accept(uow, item.id, record, decision, reason)
                changed = False
            elif target_action == "edit":
                required_field = _require_field(field_name)
                required_decision = _require_decision(decision)
                coerced = _coerce_value(required_field, value, required_decision.value)
                validation = validate_field(required_field, coerced)
                if not validation.valid:
                    raise DomainValidationError(
                        f"invalid value for {required_field}",
                        user_action="Use a value supported by the field policy.",
                        context={
                            "field": required_field,
                            "value": coerced,
                            "errors": list(validation.errors),
                        },
                    )
                claim = await self._append_user_claim(
                    uow,
                    item.id,
                    required_field,
                    coerced,
                )
                if lock:
                    await uow.locks.upsert(
                        item.id,
                        required_field,
                        coerced,
                        source_claim_id=claim.id,
                    )
                await _append_feedback(
                    uow,
                    FeedbackKind.CORRECTION,
                    item.id,
                    field=required_field,
                    system_value=required_decision.value,
                    user_value=coerced,
                    reason=reason,
                    user_claim_id=claim.id,
                    system_decision_id=required_decision.id,
                    context=_correction_context(
                        record,
                        required_decision,
                        claims,
                        coerced,
                    ),
                    created_at=self._clock(),
                )
                changed = True
            elif target_action == "lock":
                required_field = _require_field(field_name)
                required_decision = _require_decision(decision)
                await uow.locks.upsert(item.id, required_field, required_decision.value)
                await _append_feedback(
                    uow,
                    FeedbackKind.LOCK,
                    item.id,
                    field=required_field,
                    system_value=required_decision.value,
                    user_value=required_decision.value,
                    reason=reason,
                    system_decision_id=required_decision.id,
                    created_at=self._clock(),
                )
                changed = True
            elif target_action == "unlock":
                required_field = _require_field(field_name)
                removed = await uow.locks.delete(item.id, required_field)
                if not removed:
                    raise DomainValidationError(
                        "field is not locked",
                        user_action="Remove --unlock or choose a locked field.",
                        context={"field": required_field},
                    )
                await _append_feedback(
                    uow,
                    FeedbackKind.UNLOCK,
                    item.id,
                    field=required_field,
                    system_value=decision.value if decision is not None else None,
                    user_value=decision.value if decision is not None else None,
                    reason=reason,
                    system_decision_id=decision.id if decision is not None else None,
                    created_at=self._clock(),
                )
                changed = True
            elif target_action == "unknown":
                required_field = _require_field(field_name)
                await uow.locks.upsert(item.id, required_field, None)
                await _append_feedback(
                    uow,
                    FeedbackKind.CORRECTION,
                    item.id,
                    field=required_field,
                    system_value=decision.value if decision is not None else None,
                    user_value=None,
                    reason=reason,
                    system_decision_id=decision.id if decision is not None else None,
                    context=(
                        _correction_context(record, decision, claims, None)
                        if decision is not None
                        else {}
                    ),
                    created_at=self._clock(),
                )
                changed = True
            else:  # pragma: no cover - guarded above
                changed = False

        if changed:
            await self._resolve_item.resolve(item_id, mode)
        async with self._uow_factory() as uow:
            item = await uow.items.get(item_id)
            if item is None:
                raise RecordNotFound("item not found", context={"item_id": item_id})
            record = await _current_record(uow, item.id, item.decision_record_id)
            claims = await uow.claims.list_by_item(item.id)
        view = _review_view(item, record, claims, record.id)
        return ReviewView(
            item_id=view.item_id,
            status=view.status,
            fields=view.fields,
            locked_fields=view.locked_fields,
            warnings=view.warnings,
            decision_record_id=view.decision_record_id,
            explanation=None,
            action=target_action,
        )

    async def _accept(
        self,
        uow: UnitOfWork,
        item_id: ItemId,
        record: DecisionRecord,
        decision: Decision | None,
        reason: str | None,
    ) -> None:
        targets = (
            [decision]
            if decision is not None
            else [entry for entry in record.decisions if entry.subject is Subject.ITEM]
        )
        for target in targets:
            await _append_feedback(
                uow,
                FeedbackKind.APPROVAL,
                item_id,
                field=target.field,
                system_value=target.value,
                user_value=target.value,
                reason=reason,
                system_decision_id=target.id,
                created_at=self._clock(),
            )

    async def _append_user_claim(
        self,
        uow: UnitOfWork,
        item_id: ItemId,
        field: str,
        value: FieldValue,
    ) -> Claim:
        claim = Claim(
            id=ClaimId(new_uuid7()),
            item_id=item_id,
            subject=Subject.ITEM,
            field=field,
            value=value,
            source="user",
            method="manual_override/v1",
            confidence=Confidence(1.0),
            evidence=("review:manual_override",),
            notes="value supplied during a review action",
            created_at=self._clock(),
        )
        return await uow.claims.append(claim)


def _require_field(value: str | None) -> str:
    if value is None:
        raise DomainValidationError("review action requires a field")
    return value


def _require_decision(value: Decision | None) -> Decision:
    if value is None:
        raise DomainValidationError(
            "field has no decision to review",
            user_action="Resolve the item before reviewing this field.",
        )
    return value


def _review_view(
    item: Any,
    record: DecisionRecord,
    claims: Sequence[Claim],
    record_id: Any,
) -> ReviewView:
    by_id = {claim.id: claim for claim in claims}
    fields: list[ReviewFieldView] = []
    locked: list[str] = []
    for decision in record.decisions:
        if decision.subject is not Subject.ITEM:
            continue
        alternatives = tuple(
            AlternativeView(
                claim=ClaimView.from_claim(by_id[claim_id]),
                reason="not selected by the active policy",
            )
            for claim_id in decision.alternatives
            if claim_id in by_id
        )
        if decision.locked:
            locked.append(decision.field)
        fields.append(
            ReviewFieldView(
                field=decision.field,
                current=decision.value,
                alternatives=alternatives,
                confidence=decision.confidence,
                rationale=decision.rationale,
                mode=decision.mode,
                decided_by=decision.decided_by,
                locked=decision.locked,
            )
        )
    return ReviewView(
        item_id=item.id,
        status=item.status,
        fields=tuple(fields),
        locked_fields=tuple(locked),
        decision_record_id=record_id,
    )


async def _current_record(
    uow: UnitOfWork,
    item_id: ItemId,
    record_id: Any,
) -> DecisionRecord:
    if record_id is not None:
        record = await uow.decisions.get_record(record_id)
        if record is not None and record.item_id == item_id:
            return record
    records = await uow.decisions.list_records(item_id)
    if not records:
        raise ResolutionFailed(
            "item has no decision record; resolve it before reviewing",
            user_action="Run resolve or import the item first.",
            context={"item_id": item_id},
        )
    return records[-1]


def _decision_for(record: DecisionRecord, field: str | None) -> Decision | None:
    if field is None:
        return None
    decision = record.decision_for(field, subject=Subject.ITEM)
    if decision is not None:
        return decision
    if field == "artwork":
        return record.decision_for("cover", subject=Subject.ITEM)
    if field == "cover":
        return record.decision_for("artwork", subject=Subject.ITEM)
    raise RecordNotFound(
        "no decision for field",
        user_action="Use a field shown by review.",
        context={"field": field, "record_id": str(record.id)},
    )


def _coerce_value(field: str, raw: Any, current: FieldValue) -> FieldValue:
    if raw is None:
        return None
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        lowered = text.casefold()
        if lowered in {"unknown", "null", "none"} and field not in {"title", "artist", "album"}:
            return None
        if isinstance(current, bool):
            if lowered in {"true", "yes", "1"}:
                return True
            if lowered in {"false", "no", "0"}:
                return False
            raise DomainValidationError(
                f"{field} must be a boolean",
                context={"field": field, "value": raw},
            )
        if isinstance(current, int) and not isinstance(current, bool):
            try:
                return int(text)
            except ValueError as exc:
                raise DomainValidationError(
                    f"{field} must be an integer",
                    context={"field": field, "value": raw},
                ) from exc
        if isinstance(current, float):
            try:
                return float(text)
            except ValueError as exc:
                raise DomainValidationError(
                    f"{field} must be a number",
                    context={"field": field, "value": raw},
                ) from exc
        if isinstance(current, (list, tuple)) or text.startswith("["):
            if text.startswith("["):
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise DomainValidationError(
                        f"{field} must be a JSON list or comma-separated text",
                        context={"field": field, "value": raw},
                    ) from exc
                if not isinstance(parsed, list):
                    raise DomainValidationError(f"{field} must be a list")
                return [str(item).strip() for item in parsed if str(item).strip()]
            return [part.strip() for part in text.split(",") if part.strip()]
        return text
    if isinstance(raw, (list, tuple)):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, (int, float, bool)):
        return raw
    raise DomainValidationError(
        f"unsupported value type for {field}",
        context={"field": field, "actual_type": type(raw).__name__},
    )


def _correction_context(
    record: DecisionRecord,
    decision: Decision,
    claims: Sequence[Claim],
    user_value: FieldValue,
) -> dict[str, Any]:
    alternatives = [
        {
            "claim_id": str(claim.id),
            "value": claim.value,
            "source": claim.source,
            "method": claim.method,
            "confidence": float(claim.confidence),
        }
        for claim in claims
        if claim.id in decision.alternatives
    ]
    return {
        "decision_record_id": str(record.id),
        "field": decision.field,
        "system_value": decision.value,
        "user_value": user_value,
        "policy_id": decision.policy_id,
        "mode": decision.mode.value,
        "locked": decision.locked,
        "alternatives": alternatives,
    }


async def _append_feedback(
    uow: UnitOfWork,
    kind: FeedbackKind,
    item_id: ItemId,
    *,
    field: str | None,
    system_value: FieldValue,
    user_value: FieldValue,
    reason: str | None,
    created_at: datetime,
    system_decision_id: Any | None = None,
    user_claim_id: ClaimId | None = None,
    context: Mapping[str, Any] | None = None,
) -> None:
    event = FeedbackEvent(
        id=FeedbackEventId(new_uuid7()),
        kind=kind,
        item_id=item_id,
        field=field,
        system_value=system_value,
        user_value=user_value,
        scope=CorrectionScope.ITEM,
        reason=reason,
        context_snapshot=dict(context) if context else None,
        system_decision_id=system_decision_id,
        user_claim_id=user_claim_id,
        created_at=created_at,
    )
    await uow.feedback.append_event(event, applied=True)


__all__ = ["ReviewItem"]
