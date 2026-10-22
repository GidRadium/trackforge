"""SQLAlchemy 2 declarative models for the operational catalog.

The catalog is an index over immutable bytes and artifacts, never the source
of truth for media.  JSON and timestamps are stored as TEXT so the on-disk
format stays inspectable with the standard ``sqlite3`` module.
"""

from __future__ import annotations

from sqlalchemy import (
    REAL,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base shared by the models and Alembic metadata."""


class ItemRow(Base):
    __tablename__ = "items"
    __table_args__ = (
        Index("ix_items_media_id", "media_id"),
        Index("ix_items_status", "status"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    media_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("media_objects.id"),
        nullable=True,
    )
    primary_intake_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("intakes.id"),
        nullable=True,
    )
    # Collections are deferred to Plan 18, so this is intentionally not an FK.
    collection_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    kind: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'discovered'"),
    )
    identity_artifact_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("artifacts.id"),
        nullable=True,
    )
    current_decision_record_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("decision_records.id"),
        nullable=True,
    )
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class MediaObjectRow(Base):
    __tablename__ = "media_objects"
    __table_args__ = (UniqueConstraint("content_hash", name="uq_media_objects_content_hash"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    cas_path: Mapped[str] = mapped_column(Text, nullable=False)
    container: Mapped[str] = mapped_column(Text, nullable=False)
    codec: Mapped[str | None] = mapped_column(Text, nullable=True)
    bitrate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sample_rate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    channels: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration: Mapped[float | None] = mapped_column(REAL, nullable=True)
    probe_artifact_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("artifacts.id"),
        nullable=True,
    )
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class FileInstanceRow(Base):
    __tablename__ = "file_instances"
    __table_args__ = (Index("ix_file_instances_media_id", "media_id"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    media_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("media_objects.id"),
        nullable=False,
    )
    path: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    mtime: Mapped[str] = mapped_column(Text, nullable=False)
    intake_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("intakes.id"),
        nullable=True,
    )
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class IntakeRow(Base):
    __tablename__ = "intakes"
    __table_args__ = (Index("ix_intakes_item_id", "item_id"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    item_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("items.id"),
        nullable=False,
    )
    source_ref_json: Mapped[str] = mapped_column(Text, nullable=False)
    candidate_json: Mapped[str] = mapped_column(Text, nullable=False)
    source_snapshot_artifact_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("artifacts.id"),
        nullable=True,
    )
    downloader_id: Mapped[str] = mapped_column(Text, nullable=False)
    downloader_version: Mapped[str] = mapped_column(Text, nullable=False)
    media_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("media_objects.id"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class ArtifactRow(Base):
    __tablename__ = "artifacts"
    __table_args__ = (UniqueConstraint("cache_key", name="uq_artifacts_cache_key"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    producer_id: Mapped[str] = mapped_column(Text, nullable=False)
    producer_version: Mapped[str] = mapped_column(Text, nullable=False)
    cache_key: Mapped[str] = mapped_column(Text, nullable=False)
    payload_path: Mapped[str] = mapped_column(Text, nullable=False)
    payload_hash: Mapped[str] = mapped_column(Text, nullable=False)
    inputs_json: Mapped[str] = mapped_column(Text, nullable=False)
    params_json: Mapped[str] = mapped_column(Text, nullable=False)
    params_hash: Mapped[str] = mapped_column(Text, nullable=False)
    warnings_json: Mapped[str] = mapped_column(Text, nullable=False)
    license: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[str | None] = mapped_column(Text, nullable=True)


class ClaimRow(Base):
    __tablename__ = "claims"
    __table_args__ = (Index("ix_claims_item_id_field", "item_id", "field"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    item_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("items.id"),
        nullable=False,
    )
    subject_type: Mapped[str] = mapped_column(Text, nullable=False)
    subject_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    field: Mapped[str] = mapped_column(Text, nullable=False)
    value_json: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str | None] = mapped_column(Text, nullable=True)
    order_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    locale: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    method_id: Mapped[str] = mapped_column(Text, nullable=False)
    method_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(REAL, nullable=False)
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False)
    derived_from_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class ClaimEventRow(Base):
    __tablename__ = "claim_events"
    __table_args__ = (Index("ix_claim_events_claim_id_created_at", "claim_id", "created_at"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    claim_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("claims.id"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class DecisionRecordRow(Base):
    __tablename__ = "decision_records"
    __table_args__ = (Index("ix_decision_records_item_id", "item_id"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    item_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("items.id"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_versions_json: Mapped[str] = mapped_column(Text, nullable=False)
    llm_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_claim_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class DecisionRow(Base):
    __tablename__ = "decisions"
    __table_args__ = (Index("ix_decisions_record_id_field", "record_id", "field"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    record_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("decision_records.id"),
        nullable=False,
    )
    subject_type: Mapped[str] = mapped_column(Text, nullable=False)
    subject_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    field: Mapped[str] = mapped_column(Text, nullable=False)
    value_json: Mapped[str] = mapped_column(Text, nullable=False)
    chosen_claim_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("claims.id"),
        nullable=True,
    )
    alternatives_json: Mapped[str] = mapped_column(Text, nullable=False)
    derived_from_json: Mapped[str] = mapped_column(Text, nullable=False)
    policy_id: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    decided_by: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(REAL, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    locked: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("0"),
    )
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class LockRow(Base):
    __tablename__ = "locks"
    __table_args__ = (UniqueConstraint("item_id", "field", name="uq_locks_item_id_field"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    item_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("items.id"),
        nullable=False,
    )
    field: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("''"),
    )
    value_json: Mapped[str] = mapped_column(Text, nullable=False)
    source_claim_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("claims.id"),
        nullable=True,
    )
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class FeedbackEventRow(Base):
    __tablename__ = "feedback_events"
    __table_args__ = (
        Index(
            "ix_feedback_events_item_id_field_created_at",
            "item_id",
            "field",
            "created_at",
        ),
        Index("ix_feedback_events_review_session_id", "review_session_id"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    item_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("items.id"),
        nullable=False,
    )
    field: Mapped[str | None] = mapped_column(Text, nullable=True)
    system_decision_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("decisions.id"),
        nullable=True,
    )
    system_value_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_claim_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("claims.id"),
        nullable=True,
    )
    user_value_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_session_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("review_sessions.id"),
        nullable=True,
    )
    context_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    applied: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("0"),
    )


class ReviewSessionRow(Base):
    __tablename__ = "review_sessions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    started_at: Mapped[str] = mapped_column(Text, nullable=False)
    finished_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    fields_json: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'[]'"),
    )


class ReviewSessionItemRow(Base):
    __tablename__ = "review_session_items"

    session_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("review_sessions.id"),
        primary_key=True,
    )
    item_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("items.id"),
        primary_key=True,
    )


class RunRow(Base):
    __tablename__ = "runs"
    __table_args__ = (Index("ix_runs_status", "status"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    profile: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    finished_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    config_snapshot_hash: Mapped[str | None] = mapped_column(Text, nullable=True)


class RunItemRow(Base):
    __tablename__ = "run_items"

    run_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("runs.id"),
        primary_key=True,
    )
    item_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("items.id"),
        primary_key=True,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)


class StepRow(Base):
    __tablename__ = "steps"
    __table_args__ = (Index("ix_steps_run_id_status", "run_id", "status"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    run_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("runs.id"),
        nullable=False,
    )
    item_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("items.id"),
        nullable=False,
    )
    capability_id: Mapped[str] = mapped_column(Text, nullable=False)
    capability_version: Mapped[str] = mapped_column(Text, nullable=False)
    cache_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    started_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    finished_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    warnings_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'[]'"),
    )


class RenderRow(Base):
    __tablename__ = "renders"
    __table_args__ = (Index("ix_renders_item_id_status", "item_id", "status"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    item_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("items.id"),
        nullable=False,
    )
    decision_record_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("decision_records.id"),
        nullable=False,
    )
    target_format: Mapped[str] = mapped_column(Text, nullable=False)
    naming_template: Mapped[str] = mapped_column(Text, nullable=False)
    tag_mapping_version: Mapped[str] = mapped_column(Text, nullable=False)
    options_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class OutputBundleRow(Base):
    __tablename__ = "output_bundles"
    __table_args__ = (Index("ix_output_bundles_render_id", "render_id"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    item_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("items.id"),
        nullable=False,
    )
    render_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("renders.id"),
        nullable=False,
    )
    bundle_hash: Mapped[str] = mapped_column(Text, nullable=False)
    files_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class LibraryEntryRow(Base):
    __tablename__ = "library_entries"
    __table_args__ = (Index("ix_library_entries_path", "path"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    item_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("items.id"),
        nullable=False,
    )
    bundle_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("output_bundles.id"),
        nullable=False,
    )
    path: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[str] = mapped_column(Text, nullable=False)


__all__ = [
    "ArtifactRow",
    "Base",
    "ClaimEventRow",
    "ClaimRow",
    "DecisionRecordRow",
    "DecisionRow",
    "FeedbackEventRow",
    "FileInstanceRow",
    "IntakeRow",
    "ItemRow",
    "LibraryEntryRow",
    "LockRow",
    "MediaObjectRow",
    "OutputBundleRow",
    "RenderRow",
    "ReviewSessionItemRow",
    "ReviewSessionRow",
    "RunItemRow",
    "RunRow",
    "StepRow",
]
