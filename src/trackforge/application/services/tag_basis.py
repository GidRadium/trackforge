"""Playlist/album tag-basis selection and application.

``tag_basis`` decides which context supplies album, albumartist, track, date,
and cover.  Playlist context never becomes the recording artist; its owner is
only used as the album artist (or ``Various Artists``).  Applying a basis adds
collection claims and a new append-only decision record, so the original
release-oriented decisions stay available as alternatives.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import UTC, datetime
from typing import Any

from trackforge.application.dto.collections import CollectionItemContext
from trackforge.application.ports.repositories import UnitOfWork
from trackforge.application.use_cases.resolve_item import ResolveItem
from trackforge.domain.errors import DomainValidationError, RecordNotFound
from trackforge.domain.model.claims import Claim, ClaimStatus, Confidence, Subject
from trackforge.domain.model.collection import TagBasis
from trackforge.domain.model.decisions import (
    DecisionMode,
    DecisionRecord,
    FieldPolicy,
    OnMissing,
    PolicyScope,
    PolicyStrategy,
)
from trackforge.domain.model.ids import ClaimId, DecisionRecordId, ItemId, new_uuid7
from trackforge.domain.model.outputs import ResolvedMetadata

TAG_BASIS_FIELDS = ("album", "albumartist", "track", "date", "artwork")
DEFAULT_TAG_BASIS = TagBasis.ALBUM_FIRST
_TAG_BASIS_METHOD = "collection.tag_basis/1"


@dataclass(frozen=True, slots=True)
class TagBasisChoice:
    """A concrete basis after resolving ``ask``."""

    basis: TagBasis
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "warnings", tuple(self.warnings))


@dataclass(frozen=True, slots=True)
class TagBasisOverride:
    """One field value supplied by collection context."""

    field: str
    value: Any
    rationale: str


@dataclass(frozen=True, slots=True)
class TagBasisPlan:
    """Pure field-level result of applying a basis to current decisions."""

    basis: TagBasis
    overrides: tuple[TagBasisOverride, ...] = ()
    warnings: tuple[str, ...] = ()
    degraded: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "overrides", tuple(self.overrides))
        object.__setattr__(self, "warnings", tuple(self.warnings))

    @property
    def fields(self) -> dict[str, Any]:
        return {override.field: override.value for override in self.overrides}


@dataclass(frozen=True, slots=True)
class TagBasisApplication:
    """The final resolved metadata plus sidecar-ready alternatives."""

    basis: TagBasis
    resolved: ResolvedMetadata
    warnings: tuple[str, ...] = ()
    alternatives: dict[str, list[Any]] = dc_field(default_factory=dict)
    degraded: bool = False
    applied: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "alternatives", dict(self.alternatives))


def choose_tag_basis(
    requested: TagBasis | str | None,
    *,
    default: TagBasis | str = DEFAULT_TAG_BASIS,
    auto: bool = False,
    json_output: bool = False,
    prompt: Callable[[str], str] | None = None,
) -> TagBasisChoice:
    """Resolve an ``ask`` request without blocking non-interactive callers."""

    basis = _coerce_basis(requested if requested is not None else default)
    warnings: list[str] = []
    if basis is not TagBasis.ASK:
        return TagBasisChoice(basis=basis)
    if auto or json_output:
        warnings.append("tag_basis:ask: non-interactive mode defaults to album_first")
        return TagBasisChoice(basis=DEFAULT_TAG_BASIS, warnings=tuple(warnings))
    if prompt is None:
        warnings.append("tag_basis:ask: no interactive prompt; defaulted to album_first")
        return TagBasisChoice(basis=DEFAULT_TAG_BASIS, warnings=tuple(warnings))
    answer = prompt(
        "Tag basis: 1) album_first (release metadata), 2) playlist_first (collection context)"
    ).strip()
    folded = answer.casefold()
    if folded in {"1", "album", "album_first", "release"}:
        return TagBasisChoice(basis=TagBasis.ALBUM_FIRST, warnings=tuple(warnings))
    if folded in {"2", "playlist", "playlist_first", "collection"}:
        return TagBasisChoice(basis=TagBasis.PLAYLIST_FIRST, warnings=tuple(warnings))
    raise DomainValidationError(
        "tag basis must be album_first or playlist_first",
        user_action="Pass --tag-basis album_first or --tag-basis playlist_first.",
        context={"answer": answer},
    )


def plan_tag_basis(
    basis: TagBasis | str,
    context: CollectionItemContext,
    current: Mapping[str, Any],
) -> TagBasisPlan:
    """Return field overrides without mutating catalog state."""

    target = _coerce_basis(basis)
    warnings: list[str] = []
    if target is TagBasis.ASK:
        warnings.append("tag_basis:ask: defaulted to album_first during processing")
        target = TagBasis.ALBUM_FIRST

    album = context.title
    albumartist = context.owner or "Various Artists"
    tag_date = _context_date(context)
    cover = _context_cover(context)
    cover_hash = cover if _is_sha256(cover) else None

    if target is TagBasis.PLAYLIST_FIRST:
        playlist_overrides = [
            TagBasisOverride(
                "album",
                album,
                "playlist title supplies album under playlist_first",
            ),
            TagBasisOverride(
                "albumartist",
                albumartist,
                "playlist owner supplies albumartist; recording artist is unchanged",
            ),
            TagBasisOverride(
                "track",
                context.position,
                "playlist position supplies track number under playlist_first",
            ),
        ]
        if tag_date is not None:
            playlist_overrides.append(
                TagBasisOverride(
                    "date",
                    tag_date,
                    "playlist date supplies date under playlist_first",
                )
            )
        if cover_hash is not None:
            # Per-item artwork (release cover or source thumbnail) is more
            # specific than the playlist cover.  Use the playlist cover only
            # as a fallback for items that have no cover of their own.
            if _blank(current.get("artwork")):
                playlist_overrides.append(
                    TagBasisOverride(
                        "artwork",
                        cover_hash,
                        "playlist cover supplies artwork when the item has none",
                    )
                )
        elif cover:
            warnings.append("tag_basis:playlist_first: playlist cover is not a stored CAS artifact")
        else:
            warnings.append("tag_basis:playlist_first: playlist has no cover")
        return TagBasisPlan(
            basis=target,
            overrides=tuple(playlist_overrides),
            warnings=tuple(warnings),
        )

    if _release_present(current):
        return TagBasisPlan(basis=target)

    needed = _missing_tag_fields(current)
    if not needed:
        return TagBasisPlan(basis=target)
    candidates: dict[str, Any] = {
        "album": album,
        "albumartist": albumartist,
        "track": context.position,
        "date": tag_date,
        "artwork": cover_hash,
    }
    overrides = tuple(
        TagBasisOverride(
            field_name,
            candidates[field_name],
            "release metadata was incomplete; collection context used as fallback",
        )
        for field_name in TAG_BASIS_FIELDS
        if field_name in needed and candidates[field_name] is not None
    )
    if overrides:
        warnings.append(
            "tag_basis:album_first_degraded: release context is incomplete; "
            "fell back to playlist/unknown fields"
        )
    if "artwork" in needed and cover and cover_hash is None:
        warnings.append("tag_basis:album_first_degraded: playlist cover is not stored")
    return TagBasisPlan(
        basis=target,
        overrides=overrides,
        warnings=tuple(warnings),
        degraded=bool(overrides),
    )


class TagBasisService:
    """Apply a basis by appending collection claims and re-resolving the item."""

    def __init__(self, resolve_item: ResolveItem) -> None:
        self._resolve_item = resolve_item

    async def apply(
        self,
        unit_of_work_factory: Callable[[], UnitOfWork],
        item_id: ItemId,
        context: CollectionItemContext,
        *,
        mode: str | DecisionMode = DecisionMode.POLICY,
    ) -> TagBasisApplication:
        async with unit_of_work_factory() as uow:
            item = await uow.items.get(item_id)
            if item is None:
                raise RecordNotFound("item not found", context={"item_id": item_id})
            record = await _current_record(uow, item_id, item.decision_record_id)
            if record is None:
                raise RecordNotFound(
                    "item has no decision record for tag basis",
                    context={"item_id": item_id},
                )
            claims = await uow.claims.list_by_item(item_id)
            current = _record_fields(record)
            plan = plan_tag_basis(context.tag_basis, context, current)
            marker = (
                f"collection:{context.collection_id}:{context.tag_basis.value}:"
                f"position:{context.position}"
            )
            existing_claims = _existing_collection_claims(claims, marker)
            if plan.overrides and not _matches(plan, current):
                for override in plan.overrides:
                    if override.field in existing_claims:
                        continue
                    saved = await uow.claims.append(
                        Claim(
                            id=ClaimId(new_uuid7()),
                            item_id=item_id,
                            subject=Subject.ITEM,
                            field=override.field,
                            value=override.value,
                            source="collection",
                            method=_TAG_BASIS_METHOD,
                            confidence=Confidence(0.65),
                            evidence=(f"collection:{context.collection_id}",),
                            status=ClaimStatus.PROPOSED,
                            notes=marker,
                            created_at=datetime.now(UTC),
                        )
                    )
                    existing_claims[override.field] = saved

        if not plan.overrides or _matches(plan, current):
            resolved = _resolved(record)
            return TagBasisApplication(
                basis=plan.basis,
                resolved=resolved,
                warnings=plan.warnings,
                alternatives=_alternatives(claims, record, record, context),
                degraded=plan.degraded,
            )

        policies = tuple(_policy_for(override.field) for override in plan.overrides)
        resolved = await self._resolve_item.resolve(
            item_id,
            mode,
            policy_overrides=policies,
        )
        async with unit_of_work_factory() as uow:
            final_record = await _current_record(uow, item_id, None)
            final_claims = await uow.claims.list_by_item(item_id)
        if final_record is None:  # pragma: no cover - resolve just persisted it
            raise RecordNotFound(
                "tag basis decision record was not persisted",
                context={"item_id": item_id},
            )
        warnings = (*plan.warnings, *resolved.warnings)
        return TagBasisApplication(
            basis=plan.basis,
            resolved=resolved,
            warnings=tuple(dict.fromkeys(warnings)),
            alternatives=_alternatives(final_claims, record, final_record, context),
            degraded=plan.degraded,
            applied=True,
        )


async def _current_record(
    uow: UnitOfWork,
    item_id: ItemId,
    current_record_id: DecisionRecordId | None,
) -> DecisionRecord | None:
    if current_record_id is not None:
        record = await uow.decisions.get_record(current_record_id)
        if record is not None and record.item_id == item_id:
            return record
    records = await uow.decisions.list_records(item_id)
    return records[-1] if records else None


def _record_fields(record: DecisionRecord) -> dict[str, Any]:
    return {
        decision.field: decision.value
        for decision in record.decisions
        if decision.subject is Subject.ITEM
    }


def _resolved(record: DecisionRecord) -> ResolvedMetadata:
    fields = _record_fields(record)
    return ResolvedMetadata(
        item_id=record.item_id,
        decision_record_id=record.id,
        fields=fields,
        unknowns=tuple(
            decision.field
            for decision in record.decisions
            if decision.subject is Subject.ITEM and decision.value is None
        ),
    )


def _existing_collection_claims(
    claims: list[Claim],
    marker: str,
) -> dict[str, Claim]:
    result: dict[str, Claim] = {}
    for claim in claims:
        if claim.source != "collection" or claim.notes != marker:
            continue
        result.setdefault(claim.field, claim)
    return result


def _matches(plan: TagBasisPlan, current: Mapping[str, Any]) -> bool:
    return all(_same_value(current.get(field), value) for field, value in plan.fields.items())


def _same_value(left: object, right: object) -> bool:
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return tuple(left) == tuple(right)
    return left == right


def _policy_for(field: str) -> FieldPolicy:
    return FieldPolicy(
        id=f"collection.{field}",
        field=field,
        scope=PolicyScope.GLOBAL,
        strategy=PolicyStrategy.SOURCE_PRIORITY,
        priority=("collection", "musicbrainz", f"item.{field}", "unknown"),
        on_missing=OnMissing.UNKNOWN,
        version=1,
    )


def _alternatives(
    claims: list[Claim],
    before: DecisionRecord,
    after: DecisionRecord,
    context: CollectionItemContext,
) -> dict[str, list[Any]]:
    result: dict[str, list[Any]] = {}
    collection_values: dict[str, Any] = {
        "album": context.title,
        "albumartist": context.owner or "Various Artists",
        "track": context.position,
        "date": _context_date(context),
        "artwork": _context_cover(context),
    }
    for field_name in TAG_BASIS_FIELDS:
        values: list[Any] = []
        for record in (before, after):
            decision = record.decision_for(field_name)
            if decision is not None:
                _append_unique(values, decision.value)
        for claim in claims:
            if claim.field == field_name:
                _append_unique(values, claim.value)
        _append_unique(values, collection_values.get(field_name))
        result[field_name] = values
    result["cover"] = list(result["artwork"])
    return result


def _append_unique(values: list[Any], value: Any) -> None:
    if value is None:
        return
    if any(_same_value(value, existing) for existing in values):
        return
    values.append(value)


def _release_present(current: Mapping[str, Any]) -> bool:
    return bool(_text(current.get("album")) or _text(current.get("mb_release_id")))


def _missing_tag_fields(current: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(field for field in TAG_BASIS_FIELDS if _blank(current.get(field)))


def _blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _context_date(context: CollectionItemContext) -> str | None:
    for field in ("date", "release_date", "upload_date", "year"):
        for claim in reversed(context.context_claims):
            if claim.field.casefold() == field and isinstance(claim.value, str):
                value = claim.value.strip()
                if value:
                    return value
    return None


def _context_cover(context: CollectionItemContext) -> str | None:
    fallback: str | None = None
    for claim in reversed(context.context_claims):
        if claim.field.casefold() not in {"cover", "artwork", "cover_url"}:
            continue
        if not isinstance(claim.value, str):
            continue
        value = claim.value.strip()
        if not value:
            continue
        if _is_sha256(value):
            return value
        fallback = fallback or value
    return fallback


def _is_sha256(value: str | None) -> bool:
    if value is None or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return value == value.lower()


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _coerce_basis(value: TagBasis | str) -> TagBasis:
    if isinstance(value, TagBasis):
        return value
    try:
        return TagBasis(str(value).strip().casefold())
    except ValueError:
        raise DomainValidationError(
            "tag_basis must be one of: album_first, playlist_first, ask",
            context={"tag_basis": str(value)},
        ) from None


__all__ = [
    "DEFAULT_TAG_BASIS",
    "TAG_BASIS_FIELDS",
    "TagBasisApplication",
    "TagBasisChoice",
    "TagBasisOverride",
    "TagBasisPlan",
    "TagBasisService",
    "choose_tag_basis",
    "plan_tag_basis",
]
