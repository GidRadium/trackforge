"""Calculate feedback/correction statistics without claiming ground truth.

Definitions used here are deliberately explicit:

* a *reviewed decision* is an event that confirms or changes a decision
  (approval, rejection, or correction);
* a *correction rate* is ``corrections / reviewed decisions`` for a field,
  source, or method;
* an *unknown rate* is the share of persisted decisions whose value is null;
* a *review rate* is the share of catalog items with at least one feedback
  event.

Accuracy is never reported because there is no labelled ground-truth set in
the current catalog.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from trackforge.application.dto.feedback import (
    FeedbackGroupStats,
    FeedbackStatsReport,
)
from trackforge.application.ports.feedback import FeedbackJournal, NullFeedbackJournal
from trackforge.application.ports.repositories import UnitOfWork
from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model.feedback import (
    FeedbackEvent,
    FeedbackKind,
    ReviewSession,
)
from trackforge.domain.model.ids import ItemId
from trackforge.domain.policies.normalization import canonical_field

_REVIEW_KINDS = frozenset(
    {
        FeedbackKind.CORRECTION,
        FeedbackKind.APPROVAL,
        FeedbackKind.REJECTION,
    }
)
_GROUP_FIELDS = frozenset({"field", "source", "method"})


class FeedbackStats:
    """Build aggregated feedback statistics from the journal and DB index."""

    def __init__(
        self,
        journal: FeedbackJournal | None = None,
        unit_of_work_factory: Callable[[], UnitOfWork] | None = None,
        *,
        data_dir: Path | str | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._journal = journal or NullFeedbackJournal()
        self._uow_factory = unit_of_work_factory
        self._data_dir = Path(data_dir) if data_dir is not None else None
        self._clock = clock

    async def stats(
        self,
        *,
        group_by: str = "field",
        item_id: ItemId | str | None = None,
        field: str | None = None,
        since: datetime | str | None = None,
    ) -> FeedbackStatsReport:
        return await self.calculate(
            group_by=group_by,
            item_id=item_id,
            field=field,
            since=since,
        )

    async def calculate(
        self,
        *,
        group_by: str = "field",
        item_id: ItemId | str | None = None,
        field: str | None = None,
        since: datetime | str | None = None,
    ) -> FeedbackStatsReport:
        target_group = (group_by or "field").strip().lower()
        if target_group not in _GROUP_FIELDS:
            raise DomainValidationError(
                "unsupported feedback stats grouping",
                user_action="Use --group-by field, source, or method.",
                context={"group_by": group_by, "known": sorted(_GROUP_FIELDS)},
            )
        target_item = ItemId(str(item_id)) if item_id is not None else None
        target_field = (
            canonical_field(field.strip()) if isinstance(field, str) and field.strip() else None
        )
        target_since = _parse_since(since)

        warnings: list[str] = []
        journal_events = await self._journal.read_events(
            item_id=target_item,
            field=target_field,
            since=target_since,
        )
        db_events = await self._db_events(
            item_id=target_item,
            field=target_field,
            since=target_since,
            warnings=warnings,
        )
        events = _merge_events(journal_events, db_events, warnings=warnings)
        if (
            target_item is None
            and target_field is None
            and target_since is None
            and (journal_events or db_events)
        ):
            try:
                consistency = await self._journal.consistency(db_events)
            except Exception:  # pragma: no cover - best effort
                consistency = None
            if consistency is not None:
                warnings.extend(consistency.warnings)

        by_kind: dict[str, int] = {}
        by_field_total: dict[str, int] = {}
        by_source_total: dict[str, int] = {}
        by_method_total: dict[str, int] = {}
        by_field_reviews: dict[str, int] = {}
        by_field_corrections: dict[str, int] = {}
        by_field_unknowns: dict[str, int] = {}
        by_field_approvals: dict[str, int] = {}
        by_source_reviews: dict[str, int] = {}
        by_source_corrections: dict[str, int] = {}
        by_source_unknowns: dict[str, int] = {}
        by_source_approvals: dict[str, int] = {}
        by_method_reviews: dict[str, int] = {}
        by_method_corrections: dict[str, int] = {}
        by_method_unknowns: dict[str, int] = {}
        by_method_approvals: dict[str, int] = {}
        counters = {
            FeedbackKind.CORRECTION.value: 0,
            FeedbackKind.APPROVAL.value: 0,
            FeedbackKind.REJECTION.value: 0,
            FeedbackKind.LOCK.value: 0,
            FeedbackKind.UNLOCK.value: 0,
            FeedbackKind.REVIEW_NOTE.value: 0,
        }
        reviewed_items: set[str] = set()
        corrected_items: set[str] = set()

        for event in events:
            by_kind[event.kind.value] = by_kind.get(event.kind.value, 0) + 1
            if event.kind.value in counters:
                counters[event.kind.value] += 1
            reviewed_items.add(str(event.item_id))
            field_key = event.field or "unknown"
            source_key = _event_dimension(event, "source")
            method_key = _event_dimension(event, "method")
            is_review = event.kind in _REVIEW_KINDS
            is_correction = event.kind is FeedbackKind.CORRECTION
            is_unknown = is_correction and event.user_value is None
            if is_correction:
                corrected_items.add(str(event.item_id))
            for totals, key in (
                (by_field_total, field_key),
                (by_source_total, source_key),
                (by_method_total, method_key),
            ):
                totals[key] = totals.get(key, 0) + 1
            if is_review:
                by_field_reviews[field_key] = by_field_reviews.get(field_key, 0) + 1
                by_source_reviews[source_key] = by_source_reviews.get(source_key, 0) + 1
                by_method_reviews[method_key] = by_method_reviews.get(method_key, 0) + 1
            if is_correction:
                by_field_corrections[field_key] = by_field_corrections.get(field_key, 0) + 1
                by_source_corrections[source_key] = by_source_corrections.get(source_key, 0) + 1
                by_method_corrections[method_key] = by_method_corrections.get(method_key, 0) + 1
            if is_unknown:
                by_field_unknowns[field_key] = by_field_unknowns.get(field_key, 0) + 1
                by_source_unknowns[source_key] = by_source_unknowns.get(source_key, 0) + 1
                by_method_unknowns[method_key] = by_method_unknowns.get(method_key, 0) + 1
            if event.kind is FeedbackKind.APPROVAL:
                by_field_approvals[field_key] = by_field_approvals.get(field_key, 0) + 1
                by_source_approvals[source_key] = by_source_approvals.get(source_key, 0) + 1
                by_method_approvals[method_key] = by_method_approvals.get(method_key, 0) + 1

        total_decisions, unknown_decisions = await self._decision_counts(
            target_item,
            warnings,
        )
        total_items = await self._item_count(warnings)
        corrections = counters[FeedbackKind.CORRECTION.value]
        reviews = (
            counters[FeedbackKind.CORRECTION.value]
            + counters[FeedbackKind.APPROVAL.value]
            + counters[FeedbackKind.REJECTION.value]
        )

        by_field = _build_groups(
            by_field_total,
            by_field_reviews,
            by_field_corrections,
            by_field_unknowns,
            by_field_approvals,
        )
        by_source = _build_groups(
            by_source_total,
            by_source_reviews,
            by_source_corrections,
            by_source_unknowns,
            by_source_approvals,
        )
        by_method = _build_groups(
            by_method_total,
            by_method_reviews,
            by_method_corrections,
            by_method_unknowns,
            by_method_approvals,
        )
        groups = {
            "field": by_field,
            "source": by_source,
            "method": by_method,
        }[target_group]

        return FeedbackStatsReport(
            total_events=len(events),
            total=len(events),
            total_decisions=total_decisions,
            total_items=total_items,
            reviewed_items=len(reviewed_items),
            total_reviews=reviews,
            corrections=corrections,
            approvals=counters[FeedbackKind.APPROVAL.value],
            rejections=counters[FeedbackKind.REJECTION.value],
            locks=counters[FeedbackKind.LOCK.value],
            unlocks=counters[FeedbackKind.UNLOCK.value],
            review_notes=counters[FeedbackKind.REVIEW_NOTE.value],
            unknown_decisions=unknown_decisions,
            correction_rate=_rate(corrections, reviews),
            review_rate=_rate(len(reviewed_items), total_items),
            unknown_rate=_rate(unknown_decisions, total_decisions),
            accuracy=None,
            by_kind=dict(by_kind),
            by_field=by_field,
            by_source=by_source,
            by_method=by_method,
            by_field_counts=dict(by_field_total),
            by_source_counts=dict(by_source_total),
            by_method_counts=dict(by_method_total),
            group_by=target_group,
            groups=groups,
            warnings=tuple(dict.fromkeys(warnings)),
            generated_at=self._clock(),
        )

    async def _db_events(
        self,
        *,
        item_id: ItemId | None,
        field: str | None,
        since: datetime | None,
        warnings: list[str],
    ) -> list[FeedbackEvent]:
        if self._uow_factory is None:
            return []
        try:
            async with self._uow_factory() as uow:
                list_all = getattr(uow.feedback, "list_all", None)
                if list_all is not None:
                    result = await list_all(item_id=item_id, field=field, since=since)
                    return cast(list[FeedbackEvent], result)
                if item_id is not None:
                    return await uow.feedback.list_by_item(item_id)
        except Exception as exc:  # pragma: no cover - defensive boundary
            warnings.append(f"SQLite feedback index could not be read: {exc}")
        return []

    async def _decision_counts(
        self,
        item_id: ItemId | None,
        warnings: list[str],
    ) -> tuple[int, int]:
        if self._uow_factory is None:
            return 0, 0
        try:
            async with self._uow_factory() as uow:
                count_decisions = getattr(uow.decisions, "count_decisions", None)
                if count_decisions is None:
                    warnings.append(
                        "decision repository cannot count decisions; "
                        "unknown_rate uses feedback events only"
                    )
                    return 0, 0
                total = await count_decisions(item_id=item_id)
                unknown = await count_decisions(
                    item_id=item_id,
                    unknown_only=True,
                )
                return int(total), int(unknown)
        except Exception as exc:  # pragma: no cover - defensive boundary
            warnings.append(f"decision counts are unavailable: {exc}")
            return 0, 0

    async def _item_count(self, warnings: list[str]) -> int:
        if self._uow_factory is None:
            return 0
        try:
            async with self._uow_factory() as uow:
                count_items = getattr(uow.items, "count_items", None)
                if count_items is None:
                    return 0
                return int(await count_items())
        except Exception as exc:  # pragma: no cover - defensive boundary
            warnings.append(f"item count is unavailable: {exc}")
            return 0

    async def sessions(
        self,
        *,
        item_id: ItemId | str | None = None,
    ) -> list[ReviewSession]:
        if self._uow_factory is None:
            return []
        target = ItemId(str(item_id)) if item_id is not None else None
        async with self._uow_factory() as uow:
            return await uow.feedback.list_sessions(item_id=target)

    async def run(self, **kwargs: Any) -> FeedbackStatsReport:
        """Alias for callers that name use-case entry points ``run``."""

        return await self.calculate(**kwargs)


def _merge_events(
    journal_events: Sequence[FeedbackEvent],
    db_events: Sequence[FeedbackEvent],
    *,
    warnings: list[str],
) -> list[FeedbackEvent]:
    merged: dict[str, FeedbackEvent] = {}
    order: list[str] = []
    for event in journal_events:
        key = str(event.id)
        if key not in merged:
            order.append(key)
        merged[key] = event
    journal_ids = set(merged)
    db_ids = {str(event.id) for event in db_events}
    for event in db_events:
        key = str(event.id)
        if key not in merged:
            order.append(key)
            merged[key] = event
    missing_in_jsonl = db_ids - journal_ids
    missing_in_db = journal_ids - db_ids
    if missing_in_jsonl:
        warnings.append(
            f"{len(missing_in_jsonl)} feedback event(s) exist only in SQLite; "
            "statistics include the index"
        )
    if missing_in_db:
        warnings.append(
            f"{len(missing_in_db)} feedback event(s) exist only in JSONL; "
            "statistics include the append-only journal"
        )
    return [merged[key] for key in order]


def _build_groups(
    totals: Mapping[str, int],
    reviews: Mapping[str, int],
    corrections: Mapping[str, int],
    unknowns: Mapping[str, int],
    approvals: Mapping[str, int] | None = None,
) -> dict[str, FeedbackGroupStats]:
    approval_counts = approvals or {}
    keys = set(totals) | set(reviews) | set(corrections) | set(unknowns) | set(approval_counts)
    result: dict[str, FeedbackGroupStats] = {}
    for key in sorted(keys):
        review_count = reviews.get(key, 0)
        correction_count = corrections.get(key, 0)
        unknown_count = unknowns.get(key, 0)
        result[key] = FeedbackGroupStats(
            key=key,
            total=totals.get(key, 0),
            reviews=review_count,
            corrections=correction_count,
            approvals=approval_counts.get(key, 0),
            unknowns=unknown_count,
            correction_rate=_rate(correction_count, review_count),
            unknown_rate=_rate(unknown_count, review_count),
        )
    return result


def _event_dimension(event: FeedbackEvent, dimension: str) -> str:
    context = event.context_snapshot or {}
    direct = context.get(f"system_{dimension}") or context.get(f"decision_{dimension}")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    claims = context.get("claims")
    chosen_id = context.get("chosen_claim_id")
    if isinstance(claims, list):
        for claim in claims:
            if not isinstance(claim, Mapping):
                continue
            if chosen_id is not None and str(claim.get("id")) != str(chosen_id):
                continue
            value = claim.get(dimension)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return "unknown"


def _rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return max(0.0, min(1.0, numerator / denominator))


def _parse_since(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DomainValidationError(
            "since must be an ISO date or timestamp",
            user_action="Use YYYY-MM-DD or an ISO timestamp.",
            context={"since": text},
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


# Compatibility aliases.
FeedbackStatsService = FeedbackStats
GetFeedbackStats = FeedbackStats
CalculateFeedbackStats = FeedbackStats


__all__ = [
    "CalculateFeedbackStats",
    "FeedbackStats",
    "FeedbackStatsService",
    "GetFeedbackStats",
]
