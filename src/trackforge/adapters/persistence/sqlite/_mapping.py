"""Mapping helpers between SQLite rows and domain/DTO objects."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from trackforge.adapters.persistence.sqlite.models import (
    ArtifactRow,
    ClaimRow,
    CollectionItemRow,
    CollectionRow,
    DecisionRecordRow,
    DecisionRow,
    FeedbackEventRow,
    FileInstanceRow,
    IntakeRow,
    ItemRow,
    LibraryEntryRow,
    LockRow,
    MediaObjectRow,
    ModelRegistryRow,
    OutputBundleRow,
    RenderRow,
    RunItemRow,
    RunRow,
    StepRow,
)
from trackforge.application.dto.db import (
    ArtifactRecord,
    LockRecord,
    RunItemRecord,
    RunItemStatus,
    RunRecord,
    RunStatus,
    StepRecord,
    StepStatus,
)
from trackforge.application.ports.models import ModelInfo, ModelKind, ModelStatus
from trackforge.application.services.canonical_json import canonical_json
from trackforge.domain.errors import InvalidCatalogData
from trackforge.domain.model.claims import (
    Claim,
    ClaimStatus,
    Confidence,
    Subject,
)
from trackforge.domain.model.collection import (
    Collection,
    CollectionContextClaim,
    CollectionItem,
    CollectionItemStatus,
    TagBasis,
)
from trackforge.domain.model.decisions import (
    DecidedBy,
    Decision,
    DecisionMode,
    DecisionRecord,
)
from trackforge.domain.model.feedback import (
    CorrectionScope,
    FeedbackEvent,
    FeedbackKind,
    ReasonCode,
)
from trackforge.domain.model.identity import ContentKind
from trackforge.domain.model.ids import (
    ArtifactId,
    BundleId,
    CandidateId,
    ClaimId,
    CollectionId,
    DecisionId,
    DecisionRecordId,
    FeedbackEventId,
    FileInstanceId,
    IntakeId,
    ItemId,
    LibraryEntryId,
    MediaId,
    RenderId,
    ReviewSessionId,
    RunId,
    StepId,
)
from trackforge.domain.model.item import Item, ItemStatus
from trackforge.domain.model.media import (
    FileInstance,
    FileRole,
    Intake,
    IntakeStatus,
    MediaContainer,
    MediaObject,
    Mtime,
)
from trackforge.domain.model.outputs import (
    LibraryEntry,
    LibraryStatus,
    OutputBundle,
    Render,
    RenderStatus,
    TargetFormat,
)
from trackforge.domain.model.source import (
    Candidate,
    CollectionKind,
    CollectionRef,
    Format,
    LocalPath,
    SourceRef,
    TextQuery,
    Thumbnail,
    Url,
)


def to_iso_z(value: datetime) -> str:
    """Format a datetime as fixed-width, lexicographically sortable UTC text."""

    if value.tzinfo is None:
        normalized = value.replace(tzinfo=UTC)
    else:
        normalized = value.astimezone(UTC)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


def optional_iso_z(value: datetime | None) -> str | None:
    return None if value is None else to_iso_z(value)


def from_iso_z(value: str) -> datetime:
    """Parse persisted UTC text, rejecting malformed catalog timestamps."""

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise InvalidCatalogData(
            "catalog timestamp is not ISO 8601",
            context={"value": str(value)},
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def optional_from_iso_z(value: str | None) -> datetime | None:
    return None if value is None else from_iso_z(value)


def to_json(value: Any) -> str:
    """Encode a JSON-safe value using canonical deterministic JSON."""

    try:
        return canonical_json(value).decode("utf-8")
    except (TypeError, ValueError) as exc:
        raise InvalidCatalogData(
            "catalog value is not JSON-serializable",
            context={"error": str(exc)},
        ) from exc


def from_json(value: str | None, default: Any = None) -> Any:
    """Decode a persisted JSON text value."""

    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise InvalidCatalogData(
            "catalog JSON value is corrupt",
            context={"value": value[:200], "error": str(exc)},
        ) from exc


def json_mapping_from_row(value: str | None, field_name: str) -> dict[str, Any]:
    decoded = from_json(value, {})
    if not isinstance(decoded, Mapping):
        raise InvalidCatalogData(
            f"catalog {field_name} must be a JSON object",
            context={"value": str(value)[:200]},
        )
    return dict(decoded)


def json_list_from_row(value: str | None, field_name: str) -> list[Any]:
    decoded = from_json(value, [])
    if not isinstance(decoded, list):
        raise InvalidCatalogData(
            f"catalog {field_name} must be a JSON array",
            context={"value": str(value)[:200]},
        )
    return decoded


def split_method(method: str) -> tuple[str, str | None]:
    head, separator, tail = method.rpartition("/")
    if separator and head and tail:
        return head, tail
    return method, None


def join_method(method_id: str, method_version: str | None) -> str:
    return f"{method_id}/{method_version}" if method_version else method_id


def source_ref_document(ref: SourceRef) -> dict[str, Any]:
    if isinstance(ref, LocalPath):
        return {"type": "local_path", "path": ref.path}
    if isinstance(ref, Url):
        return {"type": "url", "url": ref.url, "site_hint": ref.site_hint}
    if isinstance(ref, TextQuery):
        return {"type": "text_query", "text": ref.text, "locale": ref.locale}
    if isinstance(ref, CollectionRef):
        return {
            "type": "collection",
            "kind": ref.kind.value,
            "source": source_ref_document(ref.source),
            "external_id": ref.external_id,
        }
    raise InvalidCatalogData(
        "unsupported source reference",
        context={"actual_type": type(ref).__name__},
    )


def source_ref_from_document(document: object) -> SourceRef:
    if not isinstance(document, Mapping):
        raise InvalidCatalogData("source_ref_json must be a JSON object")
    kind = document.get("type")
    if kind == "local_path":
        return LocalPath(path=_text(document, "path"))
    if kind == "url":
        site_hint = document.get("site_hint")
        return Url(
            url=_text(document, "url"),
            site_hint=site_hint if isinstance(site_hint, str) else None,
        )
    if kind == "text_query":
        locale = document.get("locale")
        return TextQuery(
            text=_text(document, "text"),
            locale=locale if isinstance(locale, str) else None,
        )
    if kind == "collection":
        external_id = document.get("external_id")
        return CollectionRef(
            kind=CollectionKind(_text(document, "kind")),
            source=source_ref_from_document(document.get("source")),
            external_id=external_id if isinstance(external_id, str) else None,
        )
    raise InvalidCatalogData(
        "source_ref_json has an unknown type",
        context={"type": str(kind)},
    )


def _text(document: Mapping[Any, Any], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value:
        raise InvalidCatalogData(
            f"source_ref_json.{key} must be a non-empty string",
        )
    return value


def candidate_document(candidate: Candidate) -> dict[str, Any]:
    return {
        "id": candidate.id,
        "source_ref": source_ref_document(candidate.source_ref),
        "external_id": candidate.external_id,
        "title": candidate.title,
        "uploader": candidate.uploader,
        "duration_seconds": candidate.duration_seconds,
        "formats": [asdict(item) for item in candidate.formats],
        "thumbnails": [asdict(item) for item in candidate.thumbnails],
        "extra": dict(candidate.extra),
    }


def candidate_from_document(document: object) -> Candidate:
    if not isinstance(document, Mapping):
        raise InvalidCatalogData("candidate_json must be a JSON object")
    raw_formats = document.get("formats", [])
    raw_thumbnails = document.get("thumbnails", [])
    if not isinstance(raw_formats, list) or not isinstance(raw_thumbnails, list):
        raise InvalidCatalogData("candidate formats/thumbnails must be arrays")
    extra = document.get("extra", {})
    if not isinstance(extra, Mapping):
        raise InvalidCatalogData("candidate extra must be a JSON object")
    duration = document.get("duration_seconds")
    uploader = document.get("uploader")
    return Candidate(
        id=CandidateId(_text(document, "id")),
        source_ref=source_ref_from_document(document.get("source_ref")),
        external_id=_text(document, "external_id"),
        title=_text(document, "title"),
        uploader=uploader if isinstance(uploader, str) else None,
        duration_seconds=duration if isinstance(duration, (int, float)) else None,
        formats=tuple(Format(**item) for item in _dicts(raw_formats, "formats")),
        thumbnails=tuple(Thumbnail(**item) for item in _dicts(raw_thumbnails, "thumbnails")),
        extra=dict(extra),
    )


def _dicts(values: list[Any], field_name: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, value in enumerate(values):
        if not isinstance(value, Mapping):
            raise InvalidCatalogData(f"candidate {field_name}[{index}] must be an object")
        result.append(dict(value))
    return result


def collection_context_claim_document(claim: CollectionContextClaim) -> dict[str, Any]:
    return {
        "field": claim.field,
        "value": claim.value,
        "source": claim.source,
        "confidence": float(claim.confidence),
        "evidence": list(claim.evidence),
    }


def collection_context_claim_from_document(document: object) -> CollectionContextClaim:
    if not isinstance(document, Mapping):
        raise InvalidCatalogData("collection context claim must be a JSON object")
    field = document.get("field")
    source = document.get("source", "collection")
    confidence = document.get("confidence", 0.5)
    evidence = document.get("evidence", [])
    if not isinstance(field, str) or not isinstance(source, str):
        raise InvalidCatalogData("collection context claim field/source must be strings")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        raise InvalidCatalogData("collection context claim confidence must be a number")
    if not isinstance(evidence, list):
        raise InvalidCatalogData("collection context claim evidence must be an array")
    return CollectionContextClaim(
        field=field,
        value=document.get("value"),
        source=source,
        confidence=float(confidence),
        evidence=tuple(str(item) for item in evidence),
    )


def collection_item_from_row(row: CollectionItemRow) -> CollectionItem:
    return CollectionItem(
        position=row.position,
        candidate=candidate_from_document(from_json(row.candidate_json, {})),
        status=CollectionItemStatus(row.status),
        item_id=ItemId(row.item_id) if row.item_id else None,
        error=row.error,
        updated_at=from_iso_z(row.updated_at),
    )


def collection_from_row(
    row: CollectionRow,
    items: list[CollectionItem],
) -> Collection:
    claims = [
        collection_context_claim_from_document(item)
        for item in json_list_from_row(row.context_claims_json, "context_claims_json")
    ]
    return Collection(
        id=CollectionId(row.id),
        kind=CollectionKind(row.kind),
        source_ref=source_ref_from_document(from_json(row.source_ref_json, {})),
        title=row.title,
        owner=row.owner,
        description=row.description,
        context_claims=tuple(claims),
        items=tuple(items),
        tag_basis=TagBasis(row.tag_basis),
        created_at=from_iso_z(row.created_at),
        warnings=tuple(
            str(item) for item in json_list_from_row(row.warnings_json, "warnings_json")
        ),
    )


def collection_context_document(collection: Collection) -> str:
    return to_json(
        [collection_context_claim_document(claim) for claim in collection.context_claims]
    )


def item_from_row(row: ItemRow) -> Item:
    return Item(
        id=ItemId(row.id),
        media_id=MediaId(row.media_id) if row.media_id else None,
        primary_intake_id=IntakeId(row.primary_intake_id) if row.primary_intake_id else None,
        collection_id=CollectionId(row.collection_id) if row.collection_id else None,
        kind=ContentKind(row.kind) if row.kind else None,
        status=ItemStatus(row.status),
        decision_record_id=(
            DecisionRecordId(row.current_decision_record_id)
            if row.current_decision_record_id
            else None
        ),
        created_at=from_iso_z(row.created_at),
        updated_at=from_iso_z(row.updated_at),
    )


def media_from_row(row: MediaObjectRow) -> MediaObject:
    return MediaObject(
        id=MediaId(row.id),
        content_hash=row.content_hash,
        size_bytes=row.size_bytes,
        cas_path=row.cas_path,
        container=MediaContainer(row.container),
        codec=row.codec,
        bitrate=row.bitrate,
        sample_rate=row.sample_rate,
        channels=row.channels,
        duration_seconds=row.duration,
        probe_artifact_id=ArtifactId(row.probe_artifact_id) if row.probe_artifact_id else None,
        created_at=from_iso_z(row.created_at),
    )


def intake_from_row(row: IntakeRow) -> Intake:
    return Intake(
        id=IntakeId(row.id),
        item_id=ItemId(row.item_id),
        source_ref=source_ref_from_document(from_json(row.source_ref_json, {})),
        candidate=candidate_from_document(from_json(row.candidate_json, {})),
        source_snapshot_artifact_id=(
            ArtifactId(row.source_snapshot_artifact_id) if row.source_snapshot_artifact_id else None
        ),
        downloader=row.downloader_id,
        downloader_version=row.downloader_version,
        media_id=MediaId(row.media_id) if row.media_id else None,
        status=IntakeStatus(row.status),
        created_at=from_iso_z(row.created_at),
    )


def claim_from_row(row: ClaimRow) -> Claim:
    evidence = tuple(str(item) for item in json_list_from_row(row.evidence_json, "evidence"))
    notes = row.notes
    evidence_reason: str | None = None
    if not evidence and notes is None:
        raise InvalidCatalogData(
            "claim has neither evidence nor an explanation",
            context={"claim_id": row.id},
        )
    return Claim(
        id=ClaimId(row.id),
        item_id=ItemId(row.item_id),
        subject=Subject(row.subject_type),
        subject_id=row.subject_id,
        field=row.field,
        value=from_json(row.value_json),
        role=row.role,
        order=row.order_index,
        locale=row.locale,
        source=row.source,
        method=join_method(row.method_id, row.method_version),
        confidence=Confidence(row.confidence),
        evidence=evidence,
        derived_from=tuple(
            ClaimId(str(item)) for item in json_list_from_row(row.derived_from_json, "derived_from")
        ),
        status=ClaimStatus(row.status),
        notes=notes,
        created_at=from_iso_z(row.created_at),
        evidence_reason=evidence_reason,
    )


def decision_from_row(row: DecisionRow) -> Decision:
    return Decision(
        id=DecisionId(row.id),
        field=row.field,
        subject=Subject(row.subject_type),
        value=from_json(row.value_json),
        chosen_claim_id=ClaimId(row.chosen_claim_id) if row.chosen_claim_id else None,
        alternatives=tuple(
            ClaimId(str(item)) for item in json_list_from_row(row.alternatives_json, "alternatives")
        ),
        derived_from=tuple(
            ClaimId(str(item)) for item in json_list_from_row(row.derived_from_json, "derived_from")
        ),
        policy_id=row.policy_id,
        mode=DecisionMode(row.mode),
        decided_by=DecidedBy(row.decided_by),
        confidence=row.confidence,
        rationale=row.rationale,
        locked=bool(row.locked),
        created_at=from_iso_z(row.created_at),
    )


def decision_record_from_row(
    row: DecisionRecordRow,
    decisions: tuple[Decision, ...],
) -> DecisionRecord:
    return DecisionRecord(
        id=DecisionRecordId(row.id),
        item_id=ItemId(row.item_id),
        version=row.version,
        decisions=decisions,
        policy_versions=tuple(
            str(item) for item in json_list_from_row(row.policy_versions_json, "policy_versions")
        ),
        llm_model=row.llm_model,
        prompt_version=row.prompt_version,
        input_claim_ids=tuple(
            ClaimId(str(item))
            for item in json_list_from_row(row.input_claim_ids_json, "input_claim_ids")
        ),
        llm_cost_usd=row.llm_cost_usd,
        llm_usage=(
            json_mapping_from_row(row.llm_usage_json, "llm_usage_json")
            if row.llm_usage_json is not None
            else None
        ),
        created_at=from_iso_z(row.created_at),
    )


def model_from_row(row: ModelRegistryRow) -> ModelInfo:
    return ModelInfo(
        id=row.id,
        version=row.version,
        kind=ModelKind(row.kind),
        license=row.license,
        status=ModelStatus(row.status),
        path=row.path,
        checksum=row.checksum,
        size_bytes=row.size_bytes,
        source=row.source,
        heavy=bool(row.heavy),
        metadata=json_mapping_from_row(row.metadata_json, "metadata_json"),
        updated_at=from_iso_z(row.updated_at),
    )


def artifact_from_row(row: ArtifactRow) -> ArtifactRecord:
    return ArtifactRecord(
        id=row.id,
        artifact_type=row.type,
        schema_version=row.schema_version,
        producer_id=row.producer_id,
        producer_version=row.producer_version,
        cache_key=row.cache_key,
        payload_path=row.payload_path,
        payload_hash=row.payload_hash,
        inputs=tuple(
            dict(item)
            for item in json_list_from_row(row.inputs_json, "inputs")
            if isinstance(item, Mapping)
        ),
        params=json_mapping_from_row(row.params_json, "params_json"),
        params_hash=row.params_hash,
        warnings=tuple(
            dict(item)
            for item in json_list_from_row(row.warnings_json, "warnings")
            if isinstance(item, Mapping)
        ),
        license=row.license,
        created_at=from_iso_z(row.created_at),
        expires_at=optional_from_iso_z(row.expires_at),
    )


def lock_from_row(row: LockRow) -> LockRecord:
    return LockRecord(
        id=row.id,
        item_id=ItemId(row.item_id),
        field=row.field,
        value=from_json(row.value_json),
        source_claim_id=ClaimId(row.source_claim_id) if row.source_claim_id else None,
        created_at=from_iso_z(row.created_at),
        updated_at=from_iso_z(row.updated_at),
    )


def feedback_from_row(row: FeedbackEventRow) -> FeedbackEvent:
    reason = row.reason_text if row.reason_text is not None else row.reason_code
    scope = CorrectionScope(row.scope) if row.scope else None
    context = from_json(row.context_snapshot_json) if row.context_snapshot_json else None
    if context is not None and not isinstance(context, Mapping):
        raise InvalidCatalogData(
            "feedback context_snapshot_json must be a JSON object",
            context={"feedback_id": row.id},
        )
    return FeedbackEvent(
        id=FeedbackEventId(row.id),
        kind=FeedbackKind(row.kind),
        item_id=ItemId(row.item_id),
        field=row.field,
        system_value=from_json(row.system_value_json),
        user_value=from_json(row.user_value_json),
        scope=scope,
        reason=reason,
        context_snapshot=dict(context) if isinstance(context, Mapping) else None,
        created_at=from_iso_z(row.created_at),
        system_decision_id=(DecisionId(row.system_decision_id) if row.system_decision_id else None),
        user_claim_id=ClaimId(row.user_claim_id) if row.user_claim_id else None,
        review_session_id=(
            ReviewSessionId(row.review_session_id) if row.review_session_id else None
        ),
        reason_code=(ReasonCode(row.reason_code) if row.reason_code is not None else None),
    )


def render_from_row(row: RenderRow) -> Render:
    return Render(
        id=RenderId(row.id),
        item_id=ItemId(row.item_id),
        decision_record_id=DecisionRecordId(row.decision_record_id),
        target_format=TargetFormat(row.target_format),
        naming_template=row.naming_template,
        tag_mapping_version=row.tag_mapping_version,
        options=json_mapping_from_row(row.options_json, "options_json"),
        status=RenderStatus(row.status),
        created_at=from_iso_z(row.created_at),
    )


def bundle_from_row(row: OutputBundleRow) -> OutputBundle:
    return OutputBundle(
        id=BundleId(row.id),
        item_id=ItemId(row.item_id),
        render_id=RenderId(row.render_id),
        files=tuple(str(item) for item in json_list_from_row(row.files_json, "files_json")),
        bundle_hash=row.bundle_hash,
        created_at=from_iso_z(row.created_at),
    )


def library_entry_from_row(row: LibraryEntryRow) -> LibraryEntry:
    return LibraryEntry(
        id=LibraryEntryId(row.id),
        item_id=ItemId(row.item_id),
        bundle_id=BundleId(row.bundle_id),
        path=row.path,
        status=LibraryStatus(row.status),
        published_at=from_iso_z(row.published_at),
    )


def run_from_row(row: RunRow) -> RunRecord:
    return RunRecord(
        id=RunId(row.id),
        profile=row.profile,
        mode=row.mode,
        status=RunStatus(row.status),
        started_at=optional_from_iso_z(row.started_at),
        finished_at=optional_from_iso_z(row.finished_at),
        error=_error_json(row.error_json),
        config_snapshot_hash=row.config_snapshot_hash,
    )


def run_item_from_row(row: RunItemRow) -> RunItemRecord:
    return RunItemRecord(
        run_id=RunId(row.run_id),
        item_id=ItemId(row.item_id),
        status=RunItemStatus(row.status),
    )


def step_from_row(row: StepRow) -> StepRecord:
    return StepRecord(
        id=StepId(row.id),
        run_id=RunId(row.run_id),
        item_id=ItemId(row.item_id),
        capability_id=row.capability_id,
        capability_version=row.capability_version,
        cache_key=row.cache_key,
        status=StepStatus(row.status),
        attempts=row.attempts,
        started_at=optional_from_iso_z(row.started_at),
        finished_at=optional_from_iso_z(row.finished_at),
        error=_error_json(row.error_json),
        warnings=_warnings_from_row(row.warnings_json),
    )


def _warnings_from_row(value: str) -> tuple[dict[str, Any] | str, ...]:
    result: list[dict[str, Any] | str] = []
    for item in json_list_from_row(value, "warnings_json"):
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, Mapping):
            result.append(dict(item))
        else:
            raise InvalidCatalogData(
                "step warnings must be JSON objects or strings",
            )
    return tuple(result)


def _error_json(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    decoded = from_json(value)
    if decoded is None:
        return None
    if not isinstance(decoded, Mapping):
        raise InvalidCatalogData("catalog error_json must be a JSON object")
    return dict(decoded)


def file_instance_from_row(row: FileInstanceRow) -> FileInstance:
    raw_mtime = from_json(row.mtime)
    mtime: Mtime
    if isinstance(raw_mtime, str):
        mtime = from_iso_z(raw_mtime)
    elif isinstance(raw_mtime, (int, float)) and not isinstance(raw_mtime, bool):
        mtime = float(raw_mtime)
    else:
        raise InvalidCatalogData(
            "file instance mtime must be an ISO timestamp or POSIX number",
            context={"file_instance_id": row.id},
        )
    return FileInstance(
        id=FileInstanceId(row.id),
        media_id=MediaId(row.media_id),
        path=row.path,
        role=FileRole(row.role),
        size_bytes=row.size_bytes,
        mtime=mtime,
        intake_id=IntakeId(row.intake_id) if row.intake_id else None,
        created_at=from_iso_z(row.created_at),
    )


def file_instance_role(value: str) -> FileRole:
    return FileRole(value)


__all__ = [
    "artifact_from_row",
    "bundle_from_row",
    "candidate_document",
    "candidate_from_document",
    "claim_from_row",
    "collection_context_claim_document",
    "collection_context_claim_from_document",
    "collection_context_document",
    "collection_from_row",
    "collection_item_from_row",
    "decision_from_row",
    "decision_record_from_row",
    "feedback_from_row",
    "file_instance_from_row",
    "file_instance_role",
    "from_iso_z",
    "from_json",
    "intake_from_row",
    "item_from_row",
    "json_list_from_row",
    "json_mapping_from_row",
    "library_entry_from_row",
    "lock_from_row",
    "media_from_row",
    "model_from_row",
    "optional_from_iso_z",
    "optional_iso_z",
    "render_from_row",
    "run_from_row",
    "run_item_from_row",
    "source_ref_document",
    "source_ref_from_document",
    "step_from_row",
    "to_iso_z",
    "to_json",
]
