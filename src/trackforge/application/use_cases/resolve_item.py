"""Apply locks, field policies, and validators to produce a DecisionRecord."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from trackforge.application.dto.db import LockRecord
from trackforge.application.dto.decisions import ResolvedMetadata
from trackforge.application.ports.llm import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    LLMTask,
    SourceSnippet,
)
from trackforge.application.ports.repositories import UnitOfWork
from trackforge.application.services.llm_guardrails import (
    AUTOPILOT_DEFAULT_FIELDS,
    LLMGuardrails,
    canonical_llm_field,
)
from trackforge.application.services.prompts import EXTRACT_FIELDS, OUTPUT_SCHEMA, PromptLibrary
from trackforge.domain.errors import (
    BudgetExceeded,
    DomainValidationError,
    InvalidStateTransition,
    LLMInvalidOutput,
    ProviderUnavailable,
    RateLimited,
    RecordNotFound,
    ResolutionFailed,
    UnsupportedMode,
)
from trackforge.domain.model.claims import Claim, ClaimStatus, Confidence, Subject
from trackforge.domain.model.decisions import (
    DecidedBy,
    Decision,
    DecisionMode,
    DecisionRecord,
    FieldPolicy,
    OnMissing,
    PolicyStrategy,
)
from trackforge.domain.model.ids import ClaimId, DecisionRecordId, ItemId, new_uuid7
from trackforge.domain.model.item import ItemStatus
from trackforge.domain.policies.normalization import (
    canonical_field,
    claim_fields_for,
    value_equal,
)
from trackforge.domain.policies.registry import (
    DEFAULT_POLICY_REGISTRY,
    PolicyRegistry,
    policy_version,
)
from trackforge.domain.policies.strategies import (
    StrategyResult,
    select_consensus,
    select_derive,
    select_keep_existing,
    select_review,
    select_source_priority,
    select_unknown,
    select_user_lock,
)
from trackforge.domain.policies.validators import (
    ValidationIssue,
    ValidationResult,
    merge_results,
    validate_cross_fields,
    validate_field,
)

_ACTIVE_CLAIM_STATUSES = frozenset({ClaimStatus.PROPOSED, ClaimStatus.ACCEPTED})
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
_AUTHORITY_SOURCES = frozenset({"musicbrainz", "acoustid"})


@dataclass(frozen=True, slots=True)
class _SelectedField:
    policy: FieldPolicy
    claims: tuple[Claim, ...]
    invalid_claims: tuple[Claim, ...]
    selection: StrategyResult
    validation: ValidationResult


class ResolveItem:
    """Resolve item claims into a new, append-only decision record."""

    def __init__(
        self,
        unit_of_work_factory: Callable[[], UnitOfWork],
        *,
        registry: PolicyRegistry = DEFAULT_POLICY_REGISTRY,
        policies: Sequence[FieldPolicy] | None = None,
        llm_provider: LLMProvider | None = None,
        guardrails: LLMGuardrails | None = None,
        prompts: PromptLibrary | None = None,
        max_cost_usd_per_item: float | None = None,
        llm_max_tokens: int = 2_000,
        llm_temperature: float = 0.1,
        llm_disabled_reason: str | None = None,
        autopilot_allowed_fields: Sequence[str] = tuple(AUTOPILOT_DEFAULT_FIELDS),
    ) -> None:
        self._uow_factory = unit_of_work_factory
        self._registry = registry.with_overrides(policies) if policies is not None else registry
        self._llm_provider = llm_provider
        self._llm_disabled_reason = llm_disabled_reason
        self._prompts = prompts or PromptLibrary()
        self._max_cost_usd_per_item = max_cost_usd_per_item
        self._llm_max_tokens = llm_max_tokens
        self._llm_temperature = llm_temperature
        self._guardrails = guardrails or LLMGuardrails(
            autopilot_allowed_fields=frozenset(autopilot_allowed_fields),
            max_cost_usd_per_item=max_cost_usd_per_item,
        )

    async def resolve(
        self,
        item_id: ItemId,
        mode: str | DecisionMode = DecisionMode.POLICY,
        policy_overrides: (
            FieldPolicy | Sequence[FieldPolicy] | Mapping[str, FieldPolicy] | None
        ) = None,
    ) -> ResolvedMetadata:
        """Resolve one item and persist a new versioned decision record."""

        target_mode = _parse_mode(mode)
        registry = self._registry.with_overrides(policy_overrides)
        warnings: list[str] = []
        created_user_claims: list[Claim] = []
        llm_metadata: dict[str, Any] = {}

        async with self._uow_factory() as uow:
            item = await uow.items.get(item_id)
            if item is None:
                raise RecordNotFound("item not found", context={"item_id": item_id})
            if item.status not in {
                ItemStatus.OBSERVED,
                ItemStatus.INTERPRETED,
                ItemStatus.NEEDS_REVIEW,
                ItemStatus.RESOLVED,
                ItemStatus.RENDERED,
                ItemStatus.PUBLISHED,
            }:
                raise InvalidStateTransition(
                    f"cannot resolve an item in status {item.status.value}",
                    user_action="Observe and interpret the item before resolving it.",
                    context={"item_id": item_id, "status": item.status.value},
                )

            claims = await uow.claims.list_by_item(item.id)
            locks = await uow.locks.list_by_item(item.id)
            _validate_lock_fields(locks, registry)
            if any(lock.field == "" for lock in locks):
                warnings.append("item-level lock is not applied to individual fields in v1")

            if target_mode is DecisionMode.POLICY and self._llm_provider is not None:
                claims = await self._extract_claims(
                    uow,
                    item.id,
                    claims,
                    warnings,
                    llm_metadata,
                )

            transformations = _transformation_claims(claims)
            if _derivative_is_ambiguous(claims):
                warnings.append(
                    "derivative_ambiguous: conflicting evidence; "
                    "title qualifier and derivative decisions were not applied"
                )
            context: dict[str, Any] = {
                "derivative": bool(transformations),
                "kind": item.kind.value if item.kind is not None else None,
            }
            policies = registry.selected(context)
            now = datetime.now(UTC)
            selected_fields: list[_SelectedField] = []
            field_decisions: dict[str, Decision] = {}

            for policy in policies:
                field = policy.field
                field_claims = _claims_for_field(claims, field)
                valid_claims, invalid_claims = _split_valid(field, field_claims)
                lock = _lock_for(field, locks)
                validation = merge_results(
                    *(validate_field(field, claim.value) for claim in field_claims)
                )
                if lock is not None:
                    lock_validation = validate_field(field, lock.value)
                    if not lock_validation.valid:
                        raise ResolutionFailed(
                            f"user lock for {field} is not valid",
                            context={
                                "item_id": item.id,
                                "field": field,
                                "errors": list(lock_validation.errors),
                            },
                        )
                    if lock.value is None:
                        selection = select_user_lock(field, None, field_claims)
                    else:
                        user_claim = await _resolve_user_claim(
                            uow,
                            item.id,
                            field,
                            lock,
                            field_claims,
                            created_user_claims,
                        )
                        selection = select_user_lock(
                            field,
                            lock.value,
                            field_claims,
                            user_claim=user_claim,
                            validation=lock_validation,
                        )
                else:
                    explicit_user_claim = next(
                        (
                            candidate
                            for candidate in reversed(valid_claims)
                            if candidate.source == "user" and candidate.value is not None
                        ),
                        None,
                    )
                    if explicit_user_claim is not None:
                        # An explicit review correction always outranks
                        # automatic evidence, but it is not a lock: a later
                        # review can change it again.
                        selection = replace(
                            select_user_lock(
                                field,
                                explicit_user_claim.value,
                                field_claims,
                                user_claim=explicit_user_claim,
                            ),
                            locked=False,
                            rationale=(
                                "explicit user correction selected; validators and "
                                "cross-field checks still apply"
                            ),
                        )
                    else:
                        selection = _select_field(
                            policy,
                            field,
                            valid_claims,
                            all_claims=field_claims,
                            transformations=transformations,
                        )
                    if selection.value is None:
                        selection = _apply_missing_policy(
                            policy,
                            field,
                            field_claims,
                            selection,
                            notifications=warnings,
                        )
                selected = _SelectedField(
                    policy=policy,
                    claims=field_claims,
                    invalid_claims=invalid_claims,
                    selection=selection,
                    validation=validation,
                )
                selected_fields.append(selected)
                field_decisions[field] = _make_decision(
                    policy=policy,
                    selection=selection,
                    field_claims=field_claims,
                    invalid_claims=invalid_claims,
                    mode=target_mode,
                    created_at=now,
                )

            if (
                target_mode in {DecisionMode.LLM_ASSISTED, DecisionMode.LLM_AUTOPILOT}
                and self._llm_provider is not None
            ):
                field_decisions = await self._apply_llm_mode(
                    field_decisions,
                    claims=claims,
                    locks=locks,
                    policies=policies,
                    target_mode=target_mode,
                    created_at=now,
                    warnings=warnings,
                    llm_metadata=llm_metadata,
                )
            elif target_mode in {
                DecisionMode.LLM_ASSISTED,
                DecisionMode.LLM_AUTOPILOT,
            }:
                warning_reason = self._llm_disabled_reason or "no provider configured"
                warnings.append(f"llm:{target_mode.value}: {warning_reason}; policy fallback")

            cross = validate_cross_fields(
                {field: decision.value for field, decision in field_decisions.items()}
            )
            cross_result = _apply_cross_field_validation(
                field_decisions,
                selected_fields,
                cross,
                target_mode=target_mode,
                created_at=now,
            )
            field_decisions = cross_result
            warnings.extend(cross.warnings)
            warnings.extend(issue.message for issue in cross.hard_issues)

            for selected in selected_fields:
                if selected.selection.value is not None:
                    warnings.extend(
                        f"{selected.policy.field}: {warning}"
                        for warning in selected.validation.warnings
                    )
                if selected.invalid_claims:
                    warnings.append(
                        f"{selected.policy.field}: "
                        f"{len(selected.invalid_claims)} invalid claim(s) rejected"
                    )
                if selected.selection.review_required:
                    warnings.append(f"{selected.policy.field}: review requested by policy")
            source_noise = {
                str(claim.value)
                for claim in claims
                if claim.field == "source_noise" and isinstance(claim.value, str)
            }
            warnings.extend(f"source_noise:{marker}" for marker in sorted(source_noise))
            has_authority_ids = any(
                claim.field in _ID_FIELDS and claim.source in _AUTHORITY_SOURCES for claim in claims
            )
            if any(claim.field in _ID_FIELDS for claim in claims) and not has_authority_ids:
                warnings.append(
                    "external IDs require authority evidence; policy mode leaves them unknown"
                )

            decisions = tuple(field_decisions[policy.field] for policy in policies)
            input_ids = _input_claim_ids(claims, created_user_claims)
            records = await uow.decisions.list_records(item.id)
            version = max((record.version for record in records), default=0) + 1
            record = DecisionRecord(
                id=DecisionRecordId(new_uuid7()),
                item_id=item.id,
                version=version,
                decisions=decisions,
                policy_versions=tuple(policy_version(policy) for policy in policies),
                llm_model=llm_metadata.get("model"),
                prompt_version=llm_metadata.get("prompt_version"),
                input_claim_ids=input_ids,
                llm_cost_usd=llm_metadata.get("cost_usd"),
                llm_usage=llm_metadata.get("usage"),
                created_at=now,
            )
            saved = await uow.decisions.save_record(record)

            item.decision_record_id = saved.id
            if item.status is ItemStatus.OBSERVED:
                item.transition_to(ItemStatus.INTERPRETED, at=now)
            if item.status in {ItemStatus.INTERPRETED, ItemStatus.NEEDS_REVIEW}:
                item.transition_to(ItemStatus.RESOLVED, at=now)
            await uow.items.save(item)

        fields = {decision.field: decision.value for decision in saved.decisions}
        unknowns = tuple(decision.field for decision in saved.decisions if decision.value is None)
        return ResolvedMetadata(
            item_id=item_id,
            decision_record_id=saved.id,
            fields=fields,
            unknowns=unknowns,
            warnings=tuple(dict.fromkeys(warnings)),
        )

    async def _extract_claims(
        self,
        uow: UnitOfWork,
        item_id: ItemId,
        claims: Sequence[Claim],
        warnings: list[str],
        llm_metadata: dict[str, Any],
    ) -> list[Claim]:
        if not _source_snippets(claims):
            warnings.append("llm:extract: skipped because there is no source text")
            return list(claims)
        response = await self._request_llm(
            task=LLMTask.EXTRACT,
            claims=claims,
            target_fields=tuple(EXTRACT_FIELDS),
            warnings=warnings,
            llm_metadata=llm_metadata,
        )
        if response is None:
            return list(claims)
        try:
            outcome = self._guardrails.check_response(
                response,
                claims=claims,
                target_fields=tuple(EXTRACT_FIELDS),
                mode=DecisionMode.POLICY,
                budget_usd=self._max_cost_usd_per_item,
            )
        except BudgetExceeded as exc:
            warnings.append(f"llm:extract:budget_exceeded: {exc.message}")
            return list(claims)
        warnings.extend(outcome.all_warnings)
        if not outcome.claims:
            return list(claims)
        existing = {
            (
                canonical_field(claim.field),
                claim.source,
                claim.method,
                _value_key(claim.value),
            )
            for claim in claims
            if claim.source == "llm"
        }
        appended = 0
        method = f"{response.model}/{response.prompt_version}"
        for proposal in outcome.claims:
            field = canonical_field(proposal.field)
            key = (field, "llm", method, _value_key(proposal.value))
            if key in existing:
                continue
            notes = proposal.reason or "LLM extracted claim"
            if proposal.quote:
                notes = f"{notes}; quote: {proposal.quote[:500]}"
            claim = Claim(
                id=ClaimId(new_uuid7()),
                item_id=item_id,
                subject=Subject.ITEM,
                field=field,
                value=proposal.value,
                source="llm",
                method=method,
                confidence=Confidence(proposal.confidence),
                evidence=proposal.evidence_claim_ids,
                notes=notes,
                created_at=datetime.now(UTC),
            )
            await uow.claims.append(claim)
            existing.add(key)
            appended += 1
        if appended:
            warnings.append(f"llm:extract: added {appended} claim(s) for policy evaluation")
            return await uow.claims.list_by_item(item_id)
        return list(claims)

    async def _apply_llm_mode(
        self,
        field_decisions: dict[str, Decision],
        *,
        claims: Sequence[Claim],
        locks: Sequence[LockRecord],
        policies: Sequence[FieldPolicy],
        target_mode: DecisionMode,
        created_at: datetime,
        warnings: list[str],
        llm_metadata: dict[str, Any],
    ) -> dict[str, Decision]:
        task = (
            LLMTask.AUTOPILOT if target_mode is DecisionMode.LLM_AUTOPILOT else LLMTask.ADJUDICATE
        )
        target_fields = tuple(policy.field for policy in policies)
        response = await self._request_llm(
            task=task,
            claims=claims,
            target_fields=target_fields,
            warnings=warnings,
            llm_metadata=llm_metadata,
        )
        if response is None:
            warnings.append(
                f"llm:{task.value}: fallback to policy decisions; no validated LLM output"
            )
            return field_decisions
        locked_fields = tuple(lock.field for lock in locks if lock.field)
        try:
            outcome = self._guardrails.check_response(
                response,
                claims=claims,
                target_fields=target_fields,
                mode=target_mode,
                locked_fields=locked_fields,
                budget_usd=self._max_cost_usd_per_item,
            )
        except BudgetExceeded as exc:
            warnings.append(f"llm:{task.value}:budget_exceeded: {exc.message}")
            return field_decisions
        warnings.extend(outcome.all_warnings)
        by_id = {str(claim.id): claim for claim in claims}
        result = dict(field_decisions)
        applied: set[str] = set()
        for proposal in outcome.decisions:
            field = canonical_llm_field(proposal.field)
            baseline = result.get(field)
            if baseline is None:
                warnings.append(f"llm:{task.value}: ignored decision for unknown field {field}")
                continue
            if baseline.locked:
                warnings.append(f"llm:{task.value}: lock retained for {field}")
                continue
            chosen_claims = [
                by_id[claim_id] for claim_id in proposal.chosen_claim_ids if claim_id in by_id
            ]
            chosen_ids = tuple(claim.id for claim in chosen_claims)
            derived_ids = tuple(ClaimId(claim_id) for claim_id in proposal.derived_from)
            authority_ids = _authority_claim_ids(field, claims)
            if authority_ids and not authority_ids.intersection((*chosen_ids, *derived_ids)):
                warnings.append(
                    f"llm:{task.value}: authority evidence retained for {field}; "
                    "LLM proposal rejected"
                )
                continue
            if proposal.value is None:
                warnings.append(f"llm:{task.value}: unknown proposal for {field}; policy fallback")
                continue
            matched = next(
                (
                    claim
                    for claim in chosen_claims
                    if value_equal(field, claim.value, proposal.value)
                ),
                None,
            )
            derived: tuple[ClaimId, ...]
            if len(chosen_claims) == 1 and matched is not None and not derived_ids:
                chosen_id = matched.id
                derived = ()
            else:
                chosen_id = None
                derived = derived_ids or chosen_ids
            if chosen_id is None and not derived:
                warnings.append(
                    f"llm:{task.value}: rejected synthesis for {field} without evidence"
                )
                continue
            alternatives: list[ClaimId] = []
            for claim_id in (
                *baseline.alternatives,
                *((baseline.chosen_claim_id,) if baseline.chosen_claim_id else ()),
                *chosen_ids,
                *derived_ids,
            ):
                if claim_id not in alternatives and claim_id not in {chosen_id, *derived}:
                    alternatives.append(claim_id)
            result[field] = Decision(
                field=field,
                subject=Subject.ITEM,
                value=proposal.value,
                chosen_claim_id=chosen_id,
                alternatives=tuple(alternatives),
                derived_from=derived,
                policy_id=baseline.policy_id,
                mode=target_mode,
                decided_by=(
                    DecidedBy.LLM
                    if target_mode is DecisionMode.LLM_AUTOPILOT
                    else DecidedBy.POLICY_LLM
                ),
                confidence=proposal.confidence,
                rationale=(
                    f"llm {task.value} ({response.model}, {response.prompt_version}): "
                    f"{proposal.reason or 'selected from claims'}"
                ),
                locked=False,
                created_at=created_at,
            )
            applied.add(field)
        missing = [
            field for field in target_fields if field not in applied and not result[field].locked
        ]
        if missing:
            preview = ", ".join(missing[:6])
            suffix = "" if len(missing) <= 6 else f" (+{len(missing) - 6})"
            warnings.append(
                f"llm:{task.value}: {len(missing)} field(s) kept policy fallback: {preview}{suffix}"
            )
        return result

    async def _request_llm(
        self,
        *,
        task: LLMTask,
        claims: Sequence[Claim],
        target_fields: Sequence[str],
        warnings: list[str],
        llm_metadata: dict[str, Any],
    ) -> LLMResponse | None:
        provider = self._llm_provider
        if provider is None:
            warning_reason = self._llm_disabled_reason or "no provider configured"
            warnings.append(f"llm:{task.value}: {warning_reason}; policy fallback")
            return None
        request = LLMRequest(
            task=task,
            schema=OUTPUT_SCHEMA,
            claims=tuple(_claim_document(claim) for claim in claims),
            source_snippets=_source_snippets(claims),
            target_fields=tuple(target_fields),
            max_tokens=self._llm_max_tokens,
            temperature=self._llm_temperature,
            max_cost_usd=self._max_cost_usd_per_item,
        )
        try:
            response = await provider.complete(request)
        except BudgetExceeded as exc:
            warnings.append(f"llm:{task.value}:budget_exceeded: {exc.message}")
            return None
        except (ProviderUnavailable, RateLimited, LLMInvalidOutput) as exc:
            warnings.append(
                f"llm:{task.value}:fallback_policy: {type(exc).__name__}: {exc.message}"
            )
            return None
        llm_metadata.update(
            {
                "model": response.model,
                "prompt_version": response.prompt_version,
                "cost_usd": response.cost_usd,
                "usage": response.usage.as_dict() or None,
            }
        )
        warnings.extend(response.warnings)
        return response


def _parse_mode(mode: str | DecisionMode) -> DecisionMode:
    if isinstance(mode, DecisionMode):
        target = mode
    elif isinstance(mode, str):
        try:
            target = DecisionMode(mode)
        except ValueError as exc:
            raise DomainValidationError(
                "unknown resolution mode",
                context={"mode": mode},
            ) from exc
    else:
        raise DomainValidationError(
            "mode must be a DecisionMode or string",
            context={"actual_type": type(mode).__name__},
        )
    if target not in {
        DecisionMode.POLICY,
        DecisionMode.LLM_ASSISTED,
        DecisionMode.LLM_AUTOPILOT,
        DecisionMode.MANUAL,
    }:
        raise UnsupportedMode(
            f"resolution mode {target.value!r} is not implemented",
            context={"mode": target.value},
        )
    return target


def _validate_lock_fields(
    locks: Sequence[LockRecord],
    registry: PolicyRegistry,
) -> None:
    for lock in locks:
        if not lock.field:
            continue
        if not registry.has_field(lock.field):
            raise DomainValidationError(
                "lock references a field without a policy",
                user_action="Remove the stale lock or add a policy for this field.",
                context={"field": lock.field, "lock_id": lock.id},
            )


def _lock_for(field: str, locks: Sequence[LockRecord]) -> LockRecord | None:
    target = canonical_field(field)
    for lock in locks:
        if lock.field and canonical_field(lock.field) == target:
            return lock
    return None


def _claims_for_field(claims: Sequence[Claim], field: str) -> tuple[Claim, ...]:
    canonical = canonical_field(field)
    names = set(claim_fields_for(canonical))
    return tuple(
        claim
        for claim in claims
        if claim.subject is Subject.ITEM
        and claim.field in names
        and claim.status in _ACTIVE_CLAIM_STATUSES
        and claim.value is not None
        and not (canonical == "artist" and claim.role not in {None, "main", "featured"})
    )


def _derivative_is_ambiguous(claims: Sequence[Claim]) -> bool:
    return any(
        claim.subject is Subject.DERIVATIVE
        and claim.field in {"status", "derivative_status"}
        and isinstance(claim.value, str)
        and claim.value.strip().casefold() == "ambiguous"
        and claim.status in _ACTIVE_CLAIM_STATUSES
        for claim in claims
    )


def _transformation_claims(claims: Sequence[Claim]) -> tuple[Claim, ...]:
    if _derivative_is_ambiguous(claims):
        # Ambiguity is a first-class outcome.  Keeping the transformation
        # claims for explainability is fine, but deriving a title from one of
        # them would silently pick a winner.
        return ()
    return tuple(
        claim
        for claim in claims
        if claim.subject is Subject.DERIVATIVE
        and claim.field in {"transformation", "transformations"}
        and _is_transformation_value(claim.value)
        and claim.status in _ACTIVE_CLAIM_STATUSES
    )


def _is_transformation_value(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, str) and item.strip() for item in value)
    )


def _split_valid(
    field: str,
    claims: Sequence[Claim],
) -> tuple[tuple[Claim, ...], tuple[Claim, ...]]:
    valid: list[Claim] = []
    invalid: list[Claim] = []
    for claim in claims:
        result = validate_field(field, claim.value)
        if result.valid:
            valid.append(claim)
        else:
            invalid.append(claim)
    return tuple(valid), tuple(invalid)


def _select_field(
    policy: FieldPolicy,
    field: str,
    valid_claims: Sequence[Claim],
    *,
    all_claims: Sequence[Claim],
    transformations: Sequence[Claim],
) -> StrategyResult:
    if policy.strategy is PolicyStrategy.UNKNOWN:
        if field in _ID_FIELDS and any(
            claim.source in _AUTHORITY_SOURCES for claim in valid_claims
        ):
            result = select_source_priority(
                field,
                valid_claims,
                ("user_lock", "user", "musicbrainz", "acoustid", "external", "unknown"),
            )
            return StrategyResult(
                field=result.field,
                value=result.value,
                selected=result.selected,
                alternatives=result.alternatives or tuple(all_claims),
                confidence=result.confidence,
                rationale=(
                    f"authority evidence selected {field}; "
                    f"{len(result.alternatives)} alternative(s) retained"
                ),
                strategy=PolicyStrategy.AUTHORITY,
                derived_from=result.derived_from,
                synthesized=result.synthesized,
                review_required=result.review_required,
                locked=result.locked,
            )
        return select_unknown(
            field,
            all_claims,
            reason=(
                "IDs require authority evidence (Plan 12); "
                "external claims are retained as alternatives"
            ),
        )
    if policy.strategy is PolicyStrategy.AUTHORITY:
        return select_unknown(
            field,
            all_claims,
            reason="authority evidence is unavailable in policy mode",
        )
    if policy.strategy in {
        PolicyStrategy.LLM_ADJUDICATE,
        PolicyStrategy.RANKER_ADJUDICATE,
    }:
        # The baseline policy selection is still produced; LLM/ranker
        # proposals are merged afterwards and validated independently.
        result = select_source_priority(field, valid_claims, policy.priority)
        return StrategyResult(
            field=result.field,
            value=result.value,
            selected=result.selected,
            alternatives=result.alternatives,
            confidence=result.confidence,
            rationale=(f"{policy.strategy.value} baseline: {result.rationale}"),
            strategy=policy.strategy,
            derived_from=result.derived_from,
            synthesized=result.synthesized,
        )
    if policy.strategy is PolicyStrategy.USER_LOCK:
        return select_source_priority(field, valid_claims, policy.priority)
    if policy.strategy is PolicyStrategy.REVIEW:
        return select_review(field, all_claims)
    if policy.strategy is PolicyStrategy.CONSENSUS:
        return select_consensus(field, valid_claims)
    if policy.strategy is PolicyStrategy.KEEP_EXISTING:
        return select_keep_existing(field, valid_claims, policy.priority)
    if policy.strategy is PolicyStrategy.DERIVE:
        base = select_source_priority(field, valid_claims, policy.priority)
        return select_derive(field, base, transformations)
    return select_source_priority(field, valid_claims, policy.priority)


def _apply_missing_policy(
    policy: FieldPolicy,
    field: str,
    claims: Sequence[Claim],
    selection: StrategyResult,
    *,
    notifications: list[str],
) -> StrategyResult:
    if policy.on_missing is OnMissing.FAIL:
        raise ResolutionFailed(
            f"field {field} has no valid evidence and policy requires failure",
            context={"field": field, "claim_count": len(claims)},
        )
    if policy.on_missing is OnMissing.REVIEW:
        notifications.append(f"{field}: review requested because evidence is missing")
        return select_review(
            field,
            claims,
            reason="policy requires review when evidence is missing",
        )
    if not claims:
        return select_unknown(field, claims, reason="no claims; unknown is a valid decision")
    if not selection.rationale:
        return select_unknown(field, claims, reason="no valid claim selected")
    return selection


async def _resolve_user_claim(
    uow: UnitOfWork,
    item_id: ItemId,
    field: str,
    lock: LockRecord,
    claims: Sequence[Claim],
    created: list[Claim],
) -> Claim:
    if lock.source_claim_id is not None:
        for claim in claims:
            if claim.id == lock.source_claim_id and value_equal(
                field,
                claim.value,
                lock.value,
            ):
                return claim
    for claim in claims:
        if claim.source == "user" and value_equal(field, claim.value, lock.value):
            return claim
    if lock.value is None:
        # A cleared lock has no user claim; callers handle the unknown value.
        raise ResolutionFailed(
            "cannot back a null user lock with a claim",
            context={"field": field, "lock_id": lock.id},
        )
    claim = Claim(
        id=ClaimId(new_uuid7()),
        item_id=item_id,
        subject=Subject.ITEM,
        field=field,
        value=lock.value,
        source="user",
        method="manual_override",
        confidence=Confidence(1.0),
        evidence=(f"lock:{lock.id}",),
        notes="user lock supplies this value",
        created_at=datetime.now(UTC),
    )
    saved = await uow.claims.append(claim)
    created.append(saved)
    return saved


def _make_decision(
    *,
    policy: FieldPolicy,
    selection: StrategyResult,
    field_claims: Sequence[Claim],
    invalid_claims: Sequence[Claim],
    mode: DecisionMode,
    created_at: datetime,
) -> Decision:
    alternatives = _alternatives(
        selection,
        claims=(*field_claims, *invalid_claims),
    )
    chosen = selection.chosen_claim_id
    value = selection.value
    if value is not None and chosen is None and not selection.derived_from:
        # Multi-value selections carry their claim IDs in derived_from; a
        # lock/strategy result without either is invalid evidence.
        raise ResolutionFailed(
            f"decision for {policy.field} has no supporting claim or derivation",
            context={"field": policy.field},
        )
    selected_ids = {claim.id for claim in selection.selected}
    user_decided = selection.locked or any(
        claim.source == "user"
        for claim in field_claims
        if claim.id in selected_ids or claim.id in selection.derived_from
    )
    return Decision(
        field=policy.field,
        subject=Subject.ITEM,
        value=value,
        chosen_claim_id=chosen,
        alternatives=alternatives,
        derived_from=selection.derived_from,
        policy_id=policy.id or policy.field,
        mode=mode,
        decided_by=DecidedBy.USER if user_decided else DecidedBy.POLICY,
        confidence=selection.confidence,
        rationale=selection.rationale,
        locked=selection.locked,
        created_at=created_at,
    )


def _alternatives(
    selection: StrategyResult,
    *,
    claims: Sequence[Claim],
) -> tuple[ClaimId, ...]:
    selected_ids = {claim.id for claim in selection.selected}
    result: list[ClaimId] = []
    for claim in claims:
        if claim.id in selected_ids:
            continue
        result.append(claim.id)
    return tuple(dict.fromkeys(result))


def _apply_cross_field_validation(
    decisions: dict[str, Decision],
    selected_fields: Sequence[_SelectedField],
    cross: ValidationResult,
    *,
    target_mode: DecisionMode,
    created_at: datetime,
) -> dict[str, Decision]:
    hard_by_field: dict[str, list[ValidationIssue]] = {}
    for issue in cross.hard_issues:
        hard_by_field.setdefault(canonical_field(issue.field), []).append(issue)
    if not hard_by_field:
        return decisions

    by_field = {selected.policy.field: selected for selected in selected_fields}
    result = dict(decisions)
    for field, issues in hard_by_field.items():
        decision = result.get(field)
        selected = by_field.get(field)
        if decision is None or selected is None:
            continue
        if decision.locked:
            raise ResolutionFailed(
                f"locked field {field} violates a cross-field validator",
                context={"field": field, "errors": [issue.message for issue in issues]},
            )
        messages = "; ".join(issue.message for issue in issues)
        alternatives = list(decision.alternatives)
        if decision.chosen_claim_id is not None:
            alternatives.append(decision.chosen_claim_id)
        result[field] = Decision(
            field=decision.field,
            subject=decision.subject,
            value=None,
            chosen_claim_id=None,
            alternatives=tuple(dict.fromkeys(alternatives)),
            derived_from=(),
            policy_id=decision.policy_id,
            mode=target_mode,
            decided_by=DecidedBy.POLICY,
            confidence=0.0,
            rationale=f"{decision.rationale}; cross-field validation failed: {messages}",
            locked=False,
            created_at=created_at,
        )
    return result


def _authority_claim_ids(field: str, claims: Sequence[Claim]) -> set[ClaimId]:
    canonical = canonical_field(field)
    return {
        claim.id
        for claim in claims
        if canonical_field(claim.field) == canonical
        and claim.source in _AUTHORITY_SOURCES
        and claim.status in _ACTIVE_CLAIM_STATUSES
        and claim.value is not None
    }


def _claim_document(claim: Claim) -> dict[str, Any]:
    return {
        "id": str(claim.id),
        "subject": claim.subject.value,
        "subject_id": claim.subject_id,
        "field": claim.field,
        "value": claim.value,
        "role": claim.role,
        "source": claim.source,
        "method": claim.method,
        "confidence": float(claim.confidence),
        "evidence": list(claim.evidence),
        "derived_from": [str(item) for item in claim.derived_from],
        "status": claim.status.value,
    }


def _source_snippets(claims: Sequence[Claim]) -> tuple[SourceSnippet, ...]:
    snippet_fields = {
        "description",
        "comment",
        "source",
        "source_title",
        "title",
        "uploader",
        "notes",
    }
    snippets: list[SourceSnippet] = []
    seen: set[tuple[str, str]] = set()
    for claim in claims:
        if claim.value is None or canonical_field(claim.field) not in snippet_fields:
            continue
        text = claim.value if isinstance(claim.value, str) else str(claim.value)
        if not text.strip():
            continue
        text = text[:4_000]
        key = (claim.source, text)
        if key in seen:
            continue
        seen.add(key)
        snippets.append(SourceSnippet(text=text, source=claim.source, label=claim.field))
        if len(snippets) >= 12:
            break
    return tuple(snippets)


def _value_key(value: object) -> str:
    if isinstance(value, list):
        return "[" + ",".join(str(item) for item in value) + "]"
    return repr(value)


def _input_claim_ids(
    claims: Sequence[Claim],
    created: Sequence[Claim],
) -> tuple[ClaimId, ...]:
    result: list[ClaimId] = []
    for claim in (*claims, *created):
        if claim.id not in result:
            result.append(claim.id)
    return tuple(result)


__all__ = ["ResolveItem"]
