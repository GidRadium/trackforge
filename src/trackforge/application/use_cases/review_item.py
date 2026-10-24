"""Review actions over resolved decisions, locks, and feedback events.

Review is a thin application service: it validates user input, appends a user
claim and/or lock, records a correction/feedback event, then asks the resolver
for a new decision record.  Rendering and publication stay explicit caller
actions so a correction never mutates the library silently.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, cast

from trackforge.application.dto.db import LockRecord
from trackforge.application.dto.decisions import AlternativeView, ClaimView
from trackforge.application.dto.feedback import (
    CorrectionView,
    LockView,
    ReviewSessionView,
)
from trackforge.application.dto.views import (
    ReviewFieldView,
    ReviewResult,
    ReviewView,
)
from trackforge.application.ports.feedback import FeedbackJournal
from trackforge.application.ports.repositories import UnitOfWork
from trackforge.application.services.context_snapshot import build_context_snapshot
from trackforge.application.use_cases.explain_item import ExplainItem
from trackforge.application.use_cases.resolve_item import ResolveItem
from trackforge.domain.errors import (
    DomainValidationError,
    FeedbackStorageError,
    RecordNotFound,
    ResolutionFailed,
    UnsupportedScope,
)
from trackforge.domain.model._validation import FieldValue, utc_now
from trackforge.domain.model.claims import Claim, Confidence, Subject
from trackforge.domain.model.decisions import Decision, DecisionRecord
from trackforge.domain.model.feedback import (
    Correction,
    CorrectionScope,
    FeedbackEvent,
    FeedbackKind,
    ReasonCode,
    ReviewSession,
)
from trackforge.domain.model.ids import (
    ClaimId,
    CorrectionId,
    DecisionId,
    FeedbackEventId,
    ItemId,
    ReviewSessionId,
    new_uuid7,
)
from trackforge.domain.policies.normalization import canonical_field
from trackforge.domain.policies.validators import validate_cross_fields, validate_field


class ReviewAction(StrEnum):
    SHOW = "show"
    ACCEPT = "accept"
    EDIT = "edit"
    LOCK = "lock"
    UNLOCK = "unlock"
    UNKNOWN = "unknown"
    WHY = "why"
    SKIP = "skip"


REVIEW_ACTIONS: tuple[str, ...] = tuple(action.value for action in ReviewAction)
_FIELD_ACTIONS = frozenset(
    {
        ReviewAction.EDIT.value,
        ReviewAction.LOCK.value,
        ReviewAction.UNLOCK.value,
        ReviewAction.UNKNOWN.value,
        ReviewAction.WHY.value,
    }
)
_MUTATING_ACTIONS = frozenset(
    {
        ReviewAction.EDIT.value,
        ReviewAction.UNKNOWN.value,
        ReviewAction.LOCK.value,
        ReviewAction.UNLOCK.value,
    }
)
_RENDER_WARNING = (
    "review updated decisions only; run `trackforge render` and `trackforge publish` "
    "(or `import --force`) to apply them to output"
)
_INT_FIELDS = frozenset({"track", "disc", "tracktotal", "disctotal"})
_FLOAT_FIELDS = frozenset({"bpm"})
_BOOL_FIELDS = frozenset({"compilation"})
_LIST_FIELDS = frozenset({"artist", "albumartist", "genre"})


@dataclass(frozen=True, slots=True)
class ReviewRequest:
    """One explicit review action, validated at the application boundary."""

    item_id: ItemId
    action: str = ReviewAction.SHOW.value
    field: str | None = None
    value: Any = None
    reason_code: str | None = None
    reason_text: str | None = None
    scope: str = CorrectionScope.ITEM.value
    lock: bool = False
    mode: str | None = None
    auto: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.item_id, str) or not self.item_id.strip():
            raise DomainValidationError("review requires an item id")
        action = str(self.action).strip().lower()
        if action not in REVIEW_ACTIONS:
            raise DomainValidationError(
                "unknown review action",
                context={"action": action, "known": list(REVIEW_ACTIONS)},
            )
        scope = str(self.scope).strip().lower()
        if scope not in {item.value for item in CorrectionScope}:
            raise DomainValidationError("unknown correction scope", context={"scope": scope})
        if scope != CorrectionScope.ITEM.value:
            raise UnsupportedScope(
                "corrections with source/artist/profile/global scope are not "
                "executable in v1; item scope is required",
                user_action="Use --scope item; broader PreferenceRules are not available yet.",
                context={"scope": scope},
            )
        reason_code = self.reason_code
        if reason_code is not None:
            reason_code = str(reason_code).strip().lower()
            try:
                ReasonCode(str(reason_code))
            except ValueError as exc:
                raise DomainValidationError(
                    "unknown correction reason code",
                    context={
                        "reason_code": reason_code,
                        "known": [reason.value for reason in ReasonCode],
                    },
                ) from exc
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "reason_code", None if reason_code is None else str(reason_code))


class ReviewItem:
    """Apply one review action and return the current review table."""

    def __init__(
        self,
        unit_of_work_factory: Callable[[], UnitOfWork],
        resolve_item: ResolveItem,
        explain_item: ExplainItem,
        *,
        journal: FeedbackJournal | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._uow_factory = unit_of_work_factory
        self._resolve_item = resolve_item
        self._explain_item = explain_item
        self._journal = journal
        self._clock = clock

    async def review(
        self,
        item_id: ItemId | ReviewRequest,
        *,
        action: str = ReviewAction.SHOW.value,
        field: str | None = None,
        value: Any = None,
        lock: bool = False,
        reason: str | None = None,
        reason_code: str | None = None,
        reason_text: str | None = None,
        scope: str = CorrectionScope.ITEM.value,
        mode: str | None = None,
        auto: bool = False,
    ) -> ReviewView:
        """Run a review action; ``show``/``why`` are side-effect free."""

        request = self._request(
            item_id,
            action=action,
            field=field,
            value=value,
            lock=lock,
            reason=reason,
            reason_code=reason_code,
            reason_text=reason_text,
            scope=scope,
            mode=mode,
            auto=auto,
        )
        target_action = (
            ReviewAction.ACCEPT.value
            if request.auto and request.action == ReviewAction.SHOW.value
            else request.action
        )
        field_name = _canonical_optional(request.field)
        if target_action in _FIELD_ACTIONS and field_name is None:
            raise DomainValidationError(
                f"review action {target_action!r} requires a field",
                user_action="Pass --field <name>.",
                context={"action": target_action},
            )
        if target_action == ReviewAction.EDIT.value:
            _require_edit_value(request.value)

        async with self._uow_factory() as uow:
            item = await uow.items.get(request.item_id)
            if item is None:
                raise RecordNotFound("item not found", context={"item_id": request.item_id})
            record = await _current_record(uow, item.id, item.decision_record_id)
            decision = _decision_for(record, field_name) if field_name else None
            claims = await uow.claims.list_by_item(item.id)
            locks = await uow.locks.list_by_item(item.id)
            if target_action == ReviewAction.WHY.value:
                required_field = _require_field(field_name)
                explanation = await self._explain_item.explain(item.id, required_field)
                view = _review_view(
                    item,
                    record,
                    claims,
                    locks,
                    focus_field=required_field,
                )
                return ReviewView(
                    **{**_view_kwargs(view), "explanation": explanation, "action": "why"}
                )
            if target_action == ReviewAction.SHOW.value:
                return _review_view(item, record, claims, locks, focus_field=field_name)
            if target_action not in _MUTATING_ACTIONS | {
                ReviewAction.ACCEPT.value,
                ReviewAction.SKIP.value,
            }:
                return _review_view(item, record, claims, locks, focus_field=field_name)

            now = self._clock()
            session = await self._start_session(
                uow,
                item.id,
                record,
                field_name,
                now,
                action=target_action,
            )
            claims = list(claims)
            source_ref = await _source_ref(uow, item)
            events: list[FeedbackEvent] = []
            corrections: list[Correction] = []
            claims_to_append: list[Claim] = []
            lock_to_apply: tuple[str, FieldValue] | None = None
            lock_removed = False
            new_claim: Claim | None = None
            correction: Correction | None = None

            if target_action == ReviewAction.ACCEPT.value:
                targets = (
                    [decision]
                    if decision is not None
                    else [entry for entry in record.decisions if entry.subject is Subject.ITEM]
                )
                if not targets:
                    raise DomainValidationError("item has no decisions to accept")
                for target in targets:
                    events.append(
                        _feedback_event(
                            FeedbackEventId(new_uuid7()),
                            FeedbackKind.APPROVAL,
                            item.id,
                            target,
                            system_value=target.value,
                            user_value=target.value,
                            reason_code=None,
                            reason_text=request.reason_text,
                            snapshot=_snapshot(
                                item,
                                record,
                                target,
                                claims,
                                target.value,
                                target.value,
                                source_ref=source_ref,
                                session_id=session.id,
                                created_at=now,
                            ),
                            review_session_id=session.id,
                            created_at=now,
                        )
                    )
            elif target_action == ReviewAction.EDIT.value:
                required_field = _require_field(field_name)
                required_decision = _require_decision(decision)
                coerced = _coerce_value(required_field, request.value, required_decision.value)
                _validate_review_value(required_field, coerced)
                _validate_cross_field_change(record, required_field, coerced)
                new_claim = _user_claim(
                    item.id,
                    required_field,
                    coerced,
                    now,
                    reason_code=request.reason_code,
                    reason_text=request.reason_text,
                )
                claims_to_append.append(new_claim)
                claims.append(new_claim)
                if request.lock:
                    lock_to_apply = (required_field, coerced)
                correction = _correction(
                    item.id,
                    required_field,
                    required_decision,
                    coerced,
                    new_claim,
                    ReviewerReason(request.reason_code, request.reason_text),
                    _snapshot(
                        item,
                        record,
                        required_decision,
                        claims,
                        required_decision.value,
                        coerced,
                        source_ref=source_ref,
                        session_id=session.id,
                        created_at=now,
                    ),
                    now,
                    session.id,
                )
                corrections.append(correction)
                events.append(_correction_event(correction, now))
            elif target_action == ReviewAction.LOCK.value:
                required_field = _require_field(field_name)
                required_decision = _require_decision(decision)
                lock_value = required_decision.value
                if request.value is not None:
                    lock_value = _coerce_value(
                        required_field,
                        request.value,
                        required_decision.value,
                    )
                    _validate_review_value(required_field, lock_value)
                _validate_cross_field_change(record, required_field, lock_value)
                lock_to_apply = (required_field, lock_value)
                events.append(
                    _feedback_event(
                        FeedbackEventId(new_uuid7()),
                        FeedbackKind.LOCK,
                        item.id,
                        required_decision,
                        system_value=required_decision.value,
                        user_value=lock_value,
                        reason_code=request.reason_code,
                        reason_text=request.reason_text,
                        snapshot=_snapshot(
                            item,
                            record,
                            required_decision,
                            claims,
                            required_decision.value,
                            lock_value,
                            source_ref=source_ref,
                            session_id=session.id,
                            created_at=now,
                        ),
                        review_session_id=session.id,
                        created_at=now,
                    )
                )
                correction = None
            elif target_action == ReviewAction.UNLOCK.value:
                required_field = _require_field(field_name)
                removed = await uow.locks.delete(item.id, required_field)
                lock_removed = removed
                existing_decision = decision
                events.append(
                    _feedback_event(
                        FeedbackEventId(new_uuid7()),
                        FeedbackKind.UNLOCK,
                        item.id,
                        existing_decision,
                        system_value=(
                            existing_decision.value if existing_decision is not None else None
                        ),
                        user_value=None,
                        reason_code=request.reason_code,
                        reason_text=request.reason_text,
                        snapshot=(
                            _snapshot(
                                item,
                                record,
                                existing_decision,
                                claims,
                                existing_decision.value,
                                None,
                                source_ref=source_ref,
                                session_id=session.id,
                                created_at=now,
                            )
                            if existing_decision is not None
                            else None
                        ),
                        review_session_id=session.id,
                        created_at=now,
                    )
                )
            elif target_action == ReviewAction.UNKNOWN.value:
                required_field = _require_field(field_name)
                required_decision = _require_decision(decision)
                new_claim = _user_claim(
                    item.id,
                    required_field,
                    None,
                    now,
                    reason_code=request.reason_code,
                    reason_text=request.reason_text,
                )
                claims_to_append.append(new_claim)
                claims.append(new_claim)
                lock_to_apply = (required_field, None)
                correction = _correction(
                    item.id,
                    required_field,
                    required_decision,
                    None,
                    new_claim,
                    ReviewerReason(request.reason_code, request.reason_text),
                    _snapshot(
                        item,
                        record,
                        required_decision,
                        claims,
                        required_decision.value,
                        None,
                        source_ref=source_ref,
                        session_id=session.id,
                        created_at=now,
                    ),
                    now,
                    session.id,
                )
                corrections.append(correction)
                events.append(_correction_event(correction, now))
            elif target_action == ReviewAction.SKIP.value:
                fields = (
                    (field_name,)
                    if field_name
                    else tuple(
                        entry.field for entry in record.decisions if entry.subject is Subject.ITEM
                    )
                )
                note = request.reason_text or (
                    f"skipped {field_name}" if field_name else "skipped review"
                )
                events.append(
                    _feedback_event(
                        FeedbackEventId(new_uuid7()),
                        FeedbackKind.REVIEW_NOTE,
                        item.id,
                        decision,
                        system_value=decision.value if decision is not None else None,
                        user_value=None,
                        reason_code=request.reason_code,
                        reason_text=note,
                        snapshot=(
                            _snapshot(
                                item,
                                record,
                                decision,
                                claims,
                                decision.value if decision is not None else None,
                                None,
                                source_ref=source_ref,
                                session_id=session.id,
                                created_at=now,
                            )
                            if decision is not None
                            else None
                        ),
                        review_session_id=session.id,
                        created_at=now,
                    )
                )
                session = _replace_session(session, fields=fields, notes=session.notes + (note,))
            else:  # pragma: no cover - guarded by ReviewRequest
                raise DomainValidationError(f"unsupported review action {target_action!r}")

            if target_action not in {ReviewAction.SKIP.value, ReviewAction.ACCEPT.value}:
                session = _replace_session(
                    session,
                    notes=(*session.notes, f"{target_action}:{field_name or 'all'}"),
                )
            # Journal first: if the JSONL append fails, no database mutation has
            # happened yet.  A DB failure after this point leaves a JSONL-only
            # event, which export reports instead of deleting history.
            if self._journal is not None and events:
                try:
                    for event in events:
                        matching = next(
                            (
                                candidate
                                for candidate in corrections
                                if str(candidate.id) == str(event.id)
                            ),
                            None,
                        )
                        await self._journal.append_event(
                            event,
                            correction=matching,
                            applied=True,
                        )
                except FeedbackStorageError:
                    raise
                except Exception as exc:  # pragma: no cover - defensive boundary
                    raise FeedbackStorageError(
                        "cannot append feedback event; review was not applied",
                        context={"item_id": item.id, "error": str(exc)},
                    ) from exc

            await uow.feedback.save_session(session)
            for claim in claims_to_append:
                await uow.claims.append(claim)
            if lock_to_apply is not None:
                target_field, target_value = lock_to_apply
                await uow.locks.upsert(
                    item.id,
                    target_field,
                    target_value,
                    source_claim_id=new_claim.id if new_claim is not None else None,
                )
            for event in events:
                await uow.feedback.append_event(event, applied=True)

        warnings = list(await self._consistency_warnings(request.item_id, events))
        if target_action in _MUTATING_ACTIONS:
            warnings.append(_RENDER_WARNING)
        if target_action == ReviewAction.UNLOCK.value and not lock_removed:
            warnings.append("field was not locked; the unlock event was recorded for history")

        should_resolve = target_action in _MUTATING_ACTIONS and not (
            target_action == ReviewAction.UNLOCK.value and not lock_removed
        )
        resolution_warnings: list[str] = []
        if should_resolve:
            resolved = await self._resolve_item.resolve(
                request.item_id,
                request.mode or "policy",
            )
            resolution_warnings = list(resolved.warnings)
        warnings.extend(resolution_warnings)

        async with self._uow_factory() as uow:
            item = await uow.items.get(request.item_id)
            if item is None:
                raise RecordNotFound("item not found", context={"item_id": request.item_id})
            record = await _current_record(uow, item.id, item.decision_record_id)
            claims = await uow.claims.list_by_item(item.id)
            locks = await uow.locks.list_by_item(item.id)
            stored_session = await uow.feedback.get_session(session.id)

        view = _review_view(
            item,
            record,
            claims,
            locks,
            focus_field=field_name,
            session=stored_session or session,
        )
        correction_view = None if correction is None else _correction_view(correction, session.id)
        lock_view = _lock_view_for(locks, field_name)
        return ReviewView(
            **{
                **_view_kwargs(view),
                "action": target_action,
                "correction": correction_view,
                "lock": lock_view,
                "session": _session_view(stored_session or session),
                "actions": REVIEW_ACTIONS,
                "requires_render": target_action in _MUTATING_ACTIONS,
                "warnings": tuple(dict.fromkeys([*view.warnings, *warnings])),
            }
        )

    async def sessions(self, item_id: ItemId) -> list[ReviewSession]:
        """Return persisted review sessions for one item."""

        async with self._uow_factory() as uow:
            return await uow.feedback.list_sessions(item_id=item_id)

    async def session(self, session_id: ReviewSessionId) -> ReviewSession | None:
        """Return one persisted review session."""

        async with self._uow_factory() as uow:
            return await uow.feedback.get_session(session_id)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _request(
        self,
        item_id: ItemId | ReviewRequest,
        *,
        action: str,
        field: str | None,
        value: Any,
        lock: bool,
        reason: str | None,
        reason_code: str | None,
        reason_text: str | None,
        scope: str,
        mode: str | None,
        auto: bool,
    ) -> ReviewRequest:
        if isinstance(item_id, ReviewRequest):
            return item_id
        return ReviewRequest(
            item_id=item_id,
            action=action,
            field=field,
            value=value,
            lock=lock,
            reason_code=reason_code,
            reason_text=reason_text if reason_text is not None else reason,
            scope=scope,
            mode=mode,
            auto=auto,
        )

    async def _start_session(
        self,
        uow: UnitOfWork,
        item_id: ItemId,
        record: DecisionRecord,
        field: str | None,
        now: datetime,
        *,
        action: str,
    ) -> ReviewSession:
        fields = (
            (field,)
            if field is not None
            else tuple(entry.field for entry in record.decisions if entry.subject is Subject.ITEM)
        )
        notes = (f"action:{action}",)
        return ReviewSession(
            id=ReviewSessionId(new_uuid7()),
            item_ids=(item_id,),
            fields=fields,
            started_at=now,
            finished_at=now,
            notes=notes,
        )

    async def _consistency_warnings(
        self,
        item_id: ItemId,
        events: Sequence[FeedbackEvent],
    ) -> list[str]:
        if self._journal is None or not events:
            return []
        try:
            async with self._uow_factory() as uow:
                db_events = await uow.feedback.list_by_item(item_id)
            report = await self._journal.consistency(db_events)
        except Exception:  # pragma: no cover - consistency is best-effort
            return ["feedback consistency check could not be completed"]
        return list(report.warnings)


# ----------------------------------------------------------------------
# Validation / coercion
# ----------------------------------------------------------------------
def _canonical_optional(value: str | None) -> str | None:
    if isinstance(value, str) and value.strip():
        return canonical_field(value)
    return None


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


def _require_edit_value(value: Any) -> None:
    if value is None:
        raise DomainValidationError(
            "review edit requires a value",
            user_action="Pass --set <value>; use --unknown to clear a field.",
        )
    if isinstance(value, str) and not value.strip():
        raise DomainValidationError(
            "review edit requires a non-empty value",
            user_action="Pass a non-empty value, or use --unknown to clear a field.",
        )
    if isinstance(value, (list, tuple)) and not any(str(item).strip() for item in value):
        raise DomainValidationError(
            "review edit requires a non-empty value",
            user_action="Pass a non-empty value, or use --unknown to clear a field.",
        )


def _validate_review_value(field: str, value: FieldValue) -> None:
    validation = validate_field(field, value)
    if not validation.valid:
        raise DomainValidationError(
            f"invalid value for {field}",
            user_action="Use a value supported by the field policy.",
            context={
                "field": field,
                "value": value,
                "errors": list(validation.errors),
            },
        )


def _validate_cross_field_change(
    record: DecisionRecord,
    field: str,
    value: FieldValue,
) -> None:
    values = {
        decision.field: decision.value
        for decision in record.decisions
        if decision.subject is Subject.ITEM
    }
    values[field] = value
    cross = validate_cross_fields(values)
    if cross.hard_issues:
        raise DomainValidationError(
            f"invalid value for {field}: cross-field validation failed",
            user_action="Adjust related fields or choose a different value.",
            context={
                "field": field,
                "value": value,
                "errors": [issue.message for issue in cross.hard_issues],
            },
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
        if isinstance(current, bool) or field in _BOOL_FIELDS:
            if lowered in {"true", "yes", "1"}:
                return True
            if lowered in {"false", "no", "0"}:
                return False
            raise DomainValidationError(
                f"{field} must be a boolean",
                context={"field": field, "value": raw},
            )
        if (isinstance(current, int) and not isinstance(current, bool)) or field in _INT_FIELDS:
            try:
                return int(text)
            except ValueError as exc:
                raise DomainValidationError(
                    f"{field} must be an integer",
                    context={"field": field, "value": raw},
                ) from exc
        if isinstance(current, float) or field in _FLOAT_FIELDS:
            try:
                return float(text)
            except ValueError as exc:
                raise DomainValidationError(
                    f"{field} must be a number",
                    context={"field": field, "value": raw},
                ) from exc
        if isinstance(current, (list, tuple)) or field in _LIST_FIELDS or text.startswith("["):
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


# ----------------------------------------------------------------------
# Event/correction construction
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ReviewerReason:
    code: str | None = None
    text: str | None = None

    @property
    def reason_code(self) -> ReasonCode:
        if self.code is None:
            return ReasonCode.OTHER
        return ReasonCode(self.code)


def _user_claim(
    item_id: ItemId,
    field: str,
    value: FieldValue,
    now: datetime,
    *,
    reason_code: str | None,
    reason_text: str | None,
) -> Claim:
    notes = "value supplied during a review action"
    if reason_code:
        notes = f"{notes}; reason_code={reason_code}"
    if reason_text:
        notes = f"{notes}; reason={reason_text}"
    return Claim(
        id=ClaimId(new_uuid7()),
        item_id=item_id,
        subject=Subject.ITEM,
        field=field,
        value=value,
        source="user",
        method="manual_override",
        confidence=Confidence(1.0),
        evidence=("review:manual_override",),
        notes=notes,
        created_at=now,
    )


def _correction(
    item_id: ItemId,
    field: str,
    decision: Decision,
    user_value: FieldValue,
    claim: Claim,
    reason: ReviewerReason,
    snapshot: Mapping[str, Any],
    now: datetime,
    session_id: ReviewSessionId,
) -> Correction:
    return Correction(
        id=CorrectionId(new_uuid7()),
        item_id=item_id,
        field=field,
        system_decision_id=_decision_id(decision),
        system_value=decision.value,
        user_value=user_value,
        user_claim_id=claim.id,
        scope=CorrectionScope.ITEM,
        reason_code=reason.reason_code,
        reason_text=reason.text,
        context_snapshot=dict(snapshot),
        created_at=now,
        applied=True,
        review_session_id=session_id,
    )


def _correction_event(correction: Correction, now: datetime) -> FeedbackEvent:
    return FeedbackEvent(
        id=FeedbackEventId(str(correction.id)),
        kind=FeedbackKind.CORRECTION,
        item_id=correction.item_id,
        field=correction.field,
        system_value=correction.system_value,
        user_value=correction.user_value,
        scope=correction.scope,
        reason=correction.reason_text or _as_reason_code(correction.reason_code).value,
        reason_code=_as_reason_code(correction.reason_code),
        context_snapshot=dict(correction.context_snapshot),
        created_at=now,
        system_decision_id=correction.system_decision_id,
        user_claim_id=correction.user_claim_id,
        review_session_id=correction.review_session_id,
    )


def _feedback_event(
    event_id: FeedbackEventId,
    kind: FeedbackKind,
    item_id: ItemId,
    decision: Decision | None,
    *,
    system_value: FieldValue,
    user_value: FieldValue,
    reason_code: str | None,
    reason_text: str | None,
    snapshot: Mapping[str, Any] | None,
    review_session_id: ReviewSessionId,
    created_at: datetime,
) -> FeedbackEvent:
    return FeedbackEvent(
        id=event_id,
        kind=kind,
        item_id=item_id,
        field=decision.field if decision is not None else None,
        system_value=system_value,
        user_value=user_value,
        scope=CorrectionScope.ITEM,
        reason=reason_text or reason_code,
        reason_code=(ReasonCode(reason_code) if reason_code is not None else None),
        context_snapshot=dict(snapshot) if snapshot is not None else None,
        system_decision_id=(_decision_id(decision) if decision is not None else None),
        review_session_id=review_session_id,
        created_at=created_at,
    )


def _snapshot(
    item: Any,
    record: DecisionRecord,
    decision: Decision | None,
    claims: Sequence[Claim],
    system_value: FieldValue,
    user_value: FieldValue,
    *,
    source_ref: object | None,
    session_id: ReviewSessionId,
    created_at: datetime,
) -> dict[str, Any]:
    if decision is None:
        return {}
    alternatives = tuple(claim for claim in claims if claim.id in set(decision.alternatives))
    return build_context_snapshot(
        item=item,
        field=decision.field,
        system_value=system_value,
        user_value=user_value,
        decision_record=record,
        decision=decision,
        claims=claims,
        alternatives=alternatives,
        policy_versions=record.policy_versions,
        model=record.llm_model,
        prompt_version=record.prompt_version,
        source_ref=source_ref,
        review_session_id=session_id,
        created_at=created_at,
    )


def _decision_id(decision: Decision | None) -> DecisionId:
    if decision is None or decision.id is None:
        raise ResolutionFailed("decision has no persisted id")
    return decision.id


def _as_reason_code(value: ReasonCode | str) -> ReasonCode:
    return value if isinstance(value, ReasonCode) else ReasonCode(str(value))


# ----------------------------------------------------------------------
# View construction
# ----------------------------------------------------------------------
def _review_view(
    item: Any,
    record: DecisionRecord,
    claims: Sequence[Claim],
    locks: Sequence[LockRecord],
    *,
    focus_field: str | None = None,
    session: ReviewSession | None = None,
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
        decision_record_id=record.id,
        lock=_lock_view_for(locks, focus_field),
        session=_session_view(session) if session is not None else None,
        actions=REVIEW_ACTIONS,
    )


def _view_kwargs(view: ReviewView) -> dict[str, Any]:
    return {
        "item_id": view.item_id,
        "status": view.status,
        "fields": view.fields,
        "locked_fields": view.locked_fields,
        "warnings": view.warnings,
        "decision_record_id": view.decision_record_id,
        "explanation": view.explanation,
        "action": view.action,
        "correction": view.correction,
        "lock": view.lock,
        "session": view.session,
        "actions": view.actions,
        "requires_render": view.requires_render,
    }


def _lock_view_for(
    locks: Sequence[LockRecord],
    field: str | None,
) -> LockView | None:
    target = canonical_field(field) if field else None
    for lock in locks:
        if target is not None and canonical_field(lock.field) == target:
            return _lock_view(lock)
    if target is None and locks:
        return _lock_view(locks[0])
    return None


def _lock_view(lock: LockRecord) -> LockView:
    return LockView(
        id=lock.id,
        item_id=lock.item_id,
        field=lock.field,
        value=lock.value,
        source_claim_id=lock.source_claim_id,
        created_at=lock.created_at,
        updated_at=lock.updated_at,
    )


def _correction_view(
    correction: Correction,
    session_id: ReviewSessionId | None = None,
) -> CorrectionView:
    return CorrectionView(
        id=correction.id,
        item_id=correction.item_id,
        field=correction.field,
        system_decision_id=correction.system_decision_id,
        system_value=correction.system_value,
        user_value=correction.user_value,
        reason_code=_as_reason_code(correction.reason_code),
        scope=correction.scope,
        reason_text=correction.reason_text,
        user_claim_id=correction.user_claim_id,
        review_session_id=correction.review_session_id or session_id,
        created_at=correction.created_at,
        applied=correction.applied,
        context_snapshot=dict(correction.context_snapshot),
    )


def _session_view(session: ReviewSession) -> ReviewSessionView:
    return ReviewSessionView(
        id=session.id,
        item_ids=session.item_ids,
        fields=session.fields,
        started_at=session.started_at,
        finished_at=session.finished_at,
        corrections=session.corrections,
        notes=session.notes,
    )


def _replace_session(session: ReviewSession, **changes: Any) -> ReviewSession:
    from dataclasses import replace

    return replace(session, **changes)


async def _source_ref(uow: UnitOfWork, item: Any) -> object | None:
    intakes_repo = getattr(uow, "intakes", None)
    if intakes_repo is None:
        return None
    intakes = await intakes_repo.find_by_item(item.id)
    if not intakes:
        return None
    if item.primary_intake_id is not None:
        for intake in intakes:
            if intake.id == item.primary_intake_id:
                return cast(object, intake.source_ref)
    return cast(object, intakes[0].source_ref)


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
    raise DomainValidationError(
        "no decision for field",
        user_action="Use a field shown by review.",
        context={"field": field, "record_id": str(record.id)},
    )


__all__ = [
    "REVIEW_ACTIONS",
    "ReviewAction",
    "ReviewItem",
    "ReviewRequest",
    "ReviewResult",
]
