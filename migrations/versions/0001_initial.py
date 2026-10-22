"""Initial TrackForge catalog schema.

Revision ID: 0001
Revises:
Create Date: 2026-10-22

The schema is intentionally limited to the MVP operational index: identity
entities stay artifact-only, and collections/playlists/model_registry are
introduced by later plans.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "artifacts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("producer_id", sa.Text(), nullable=False),
        sa.Column("producer_version", sa.Text(), nullable=False),
        sa.Column("cache_key", sa.Text(), nullable=False),
        sa.Column("payload_path", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.Text(), nullable=False),
        sa.Column("inputs_json", sa.Text(), nullable=False),
        sa.Column("params_json", sa.Text(), nullable=False),
        sa.Column("params_hash", sa.Text(), nullable=False),
        sa.Column("warnings_json", sa.Text(), nullable=False),
        sa.Column("license", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=True),
        sa.UniqueConstraint("cache_key", name="uq_artifacts_cache_key"),
    )

    op.create_table(
        "media_objects",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("cas_path", sa.Text(), nullable=False),
        sa.Column("container", sa.Text(), nullable=False),
        sa.Column("codec", sa.Text(), nullable=True),
        sa.Column("bitrate", sa.Integer(), nullable=True),
        sa.Column("sample_rate", sa.Integer(), nullable=True),
        sa.Column("channels", sa.Integer(), nullable=True),
        sa.Column("duration", sa.REAL(), nullable=True),
        sa.Column("probe_artifact_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["probe_artifact_id"], ["artifacts.id"]),
        sa.UniqueConstraint("content_hash", name="uq_media_objects_content_hash"),
    )

    # items, intakes, and decision_records form a cycle of nullable references;
    # SQLite accepts forward references and the application fills them in order.
    op.create_table(
        "items",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("media_id", sa.Text(), nullable=True),
        sa.Column("primary_intake_id", sa.Text(), nullable=True),
        sa.Column("collection_id", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'discovered'")),
        sa.Column("identity_artifact_id", sa.Text(), nullable=True),
        sa.Column("current_decision_record_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["media_id"], ["media_objects.id"]),
        sa.ForeignKeyConstraint(["primary_intake_id"], ["intakes.id"]),
        sa.ForeignKeyConstraint(["identity_artifact_id"], ["artifacts.id"]),
        sa.ForeignKeyConstraint(["current_decision_record_id"], ["decision_records.id"]),
    )
    op.create_index("ix_items_media_id", "items", ["media_id"])
    op.create_index("ix_items_status", "items", ["status"])

    op.create_table(
        "intakes",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("item_id", sa.Text(), nullable=False),
        sa.Column("source_ref_json", sa.Text(), nullable=False),
        sa.Column("candidate_json", sa.Text(), nullable=False),
        sa.Column("source_snapshot_artifact_id", sa.Text(), nullable=True),
        sa.Column("downloader_id", sa.Text(), nullable=False),
        sa.Column("downloader_version", sa.Text(), nullable=False),
        sa.Column("media_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["item_id"], ["items.id"]),
        sa.ForeignKeyConstraint(["source_snapshot_artifact_id"], ["artifacts.id"]),
        sa.ForeignKeyConstraint(["media_id"], ["media_objects.id"]),
    )
    op.create_index("ix_intakes_item_id", "intakes", ["item_id"])

    op.create_table(
        "file_instances",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("media_id", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("mtime", sa.Text(), nullable=False),
        sa.Column("intake_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["media_id"], ["media_objects.id"]),
        sa.ForeignKeyConstraint(["intake_id"], ["intakes.id"]),
    )
    op.create_index("ix_file_instances_media_id", "file_instances", ["media_id"])

    op.create_table(
        "claims",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("item_id", sa.Text(), nullable=False),
        sa.Column("subject_type", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.Text(), nullable=True),
        sa.Column("field", sa.Text(), nullable=False),
        sa.Column("value_json", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=True),
        sa.Column("order_index", sa.Integer(), nullable=True),
        sa.Column("locale", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("method_id", sa.Text(), nullable=False),
        sa.Column("method_version", sa.Text(), nullable=True),
        sa.Column("confidence", sa.REAL(), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=False),
        sa.Column("derived_from_json", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["item_id"], ["items.id"]),
    )
    op.create_index("ix_claims_item_id_field", "claims", ["item_id", "field"])

    op.create_table(
        "claim_events",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("claim_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["claim_id"], ["claims.id"]),
    )
    op.create_index(
        "ix_claim_events_claim_id_created_at",
        "claim_events",
        ["claim_id", "created_at"],
    )

    op.create_table(
        "decision_records",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("item_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("policy_versions_json", sa.Text(), nullable=False),
        sa.Column("llm_model", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.Text(), nullable=True),
        sa.Column("input_claim_ids_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["item_id"], ["items.id"]),
    )
    op.create_index("ix_decision_records_item_id", "decision_records", ["item_id"])

    op.create_table(
        "decisions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("record_id", sa.Text(), nullable=False),
        sa.Column("subject_type", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.Text(), nullable=True),
        sa.Column("field", sa.Text(), nullable=False),
        sa.Column("value_json", sa.Text(), nullable=False),
        sa.Column("chosen_claim_id", sa.Text(), nullable=True),
        sa.Column("alternatives_json", sa.Text(), nullable=False),
        sa.Column("derived_from_json", sa.Text(), nullable=False),
        sa.Column("policy_id", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("decided_by", sa.Text(), nullable=False),
        sa.Column("confidence", sa.REAL(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("locked", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["record_id"], ["decision_records.id"]),
        sa.ForeignKeyConstraint(["chosen_claim_id"], ["claims.id"]),
    )
    op.create_index("ix_decisions_record_id_field", "decisions", ["record_id", "field"])

    op.create_table(
        "locks",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("item_id", sa.Text(), nullable=False),
        sa.Column("field", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("value_json", sa.Text(), nullable=False),
        sa.Column("source_claim_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["item_id"], ["items.id"]),
        sa.ForeignKeyConstraint(["source_claim_id"], ["claims.id"]),
        sa.UniqueConstraint("item_id", "field", name="uq_locks_item_id_field"),
    )

    op.create_table(
        "review_sessions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("started_at", sa.Text(), nullable=False),
        sa.Column("finished_at", sa.Text(), nullable=True),
        sa.Column("fields_json", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False, server_default=sa.text("'[]'")),
    )

    op.create_table(
        "feedback_events",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("item_id", sa.Text(), nullable=False),
        sa.Column("field", sa.Text(), nullable=True),
        sa.Column("system_decision_id", sa.Text(), nullable=True),
        sa.Column("system_value_json", sa.Text(), nullable=True),
        sa.Column("user_claim_id", sa.Text(), nullable=True),
        sa.Column("user_value_json", sa.Text(), nullable=True),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column("reason_code", sa.Text(), nullable=True),
        sa.Column("reason_text", sa.Text(), nullable=True),
        sa.Column("review_session_id", sa.Text(), nullable=True),
        sa.Column("context_snapshot_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("applied", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.ForeignKeyConstraint(["item_id"], ["items.id"]),
        sa.ForeignKeyConstraint(["system_decision_id"], ["decisions.id"]),
        sa.ForeignKeyConstraint(["user_claim_id"], ["claims.id"]),
        sa.ForeignKeyConstraint(["review_session_id"], ["review_sessions.id"]),
    )
    op.create_index(
        "ix_feedback_events_item_id_field_created_at",
        "feedback_events",
        ["item_id", "field", "created_at"],
    )
    op.create_index(
        "ix_feedback_events_review_session_id",
        "feedback_events",
        ["review_session_id"],
    )

    op.create_table(
        "review_session_items",
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("item_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["review_sessions.id"]),
        sa.ForeignKeyConstraint(["item_id"], ["items.id"]),
        sa.PrimaryKeyConstraint("session_id", "item_id"),
    )

    op.create_table(
        "runs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("profile", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("started_at", sa.Text(), nullable=True),
        sa.Column("finished_at", sa.Text(), nullable=True),
        sa.Column("error_json", sa.Text(), nullable=True),
        sa.Column("config_snapshot_hash", sa.Text(), nullable=True),
    )
    op.create_index("ix_runs_status", "runs", ["status"])

    op.create_table(
        "run_items",
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("item_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.ForeignKeyConstraint(["item_id"], ["items.id"]),
        sa.PrimaryKeyConstraint("run_id", "item_id"),
    )

    op.create_table(
        "steps",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("item_id", sa.Text(), nullable=False),
        sa.Column("capability_id", sa.Text(), nullable=False),
        sa.Column("capability_version", sa.Text(), nullable=False),
        sa.Column("cache_key", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("started_at", sa.Text(), nullable=True),
        sa.Column("finished_at", sa.Text(), nullable=True),
        sa.Column("error_json", sa.Text(), nullable=True),
        sa.Column("warnings_json", sa.Text(), nullable=False, server_default=sa.text("'[]'")),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.ForeignKeyConstraint(["item_id"], ["items.id"]),
    )
    op.create_index("ix_steps_run_id_status", "steps", ["run_id", "status"])

    op.create_table(
        "renders",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("item_id", sa.Text(), nullable=False),
        sa.Column("decision_record_id", sa.Text(), nullable=False),
        sa.Column("target_format", sa.Text(), nullable=False),
        sa.Column("naming_template", sa.Text(), nullable=False),
        sa.Column("tag_mapping_version", sa.Text(), nullable=False),
        sa.Column("options_json", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["item_id"], ["items.id"]),
        sa.ForeignKeyConstraint(["decision_record_id"], ["decision_records.id"]),
    )
    op.create_index("ix_renders_item_id_status", "renders", ["item_id", "status"])

    op.create_table(
        "output_bundles",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("item_id", sa.Text(), nullable=False),
        sa.Column("render_id", sa.Text(), nullable=False),
        sa.Column("bundle_hash", sa.Text(), nullable=False),
        sa.Column("files_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["item_id"], ["items.id"]),
        sa.ForeignKeyConstraint(["render_id"], ["renders.id"]),
    )
    op.create_index("ix_output_bundles_render_id", "output_bundles", ["render_id"])

    op.create_table(
        "library_entries",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("item_id", sa.Text(), nullable=False),
        sa.Column("bundle_id", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("published_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["item_id"], ["items.id"]),
        sa.ForeignKeyConstraint(["bundle_id"], ["output_bundles.id"]),
    )
    op.create_index("ix_library_entries_path", "library_entries", ["path"])


def downgrade() -> None:
    op.drop_index("ix_library_entries_path", table_name="library_entries")
    op.drop_table("library_entries")
    op.drop_index("ix_output_bundles_render_id", table_name="output_bundles")
    op.drop_table("output_bundles")
    op.drop_index("ix_renders_item_id_status", table_name="renders")
    op.drop_table("renders")
    op.drop_index("ix_steps_run_id_status", table_name="steps")
    op.drop_table("steps")
    op.drop_table("run_items")
    op.drop_index("ix_runs_status", table_name="runs")
    op.drop_table("runs")
    op.drop_table("review_session_items")
    op.drop_index(
        "ix_feedback_events_review_session_id",
        table_name="feedback_events",
    )
    op.drop_index(
        "ix_feedback_events_item_id_field_created_at",
        table_name="feedback_events",
    )
    op.drop_table("feedback_events")
    op.drop_table("review_sessions")
    op.drop_table("locks")
    op.drop_index("ix_decisions_record_id_field", table_name="decisions")
    op.drop_table("decisions")
    op.drop_index("ix_claim_events_claim_id_created_at", table_name="claim_events")
    op.drop_table("claim_events")
    op.drop_index("ix_claims_item_id_field", table_name="claims")
    op.drop_table("claims")
    op.drop_index("ix_file_instances_media_id", table_name="file_instances")
    op.drop_table("file_instances")
    op.drop_index("ix_intakes_item_id", table_name="intakes")
    op.drop_table("intakes")
    op.drop_index("ix_items_status", table_name="items")
    op.drop_index("ix_items_media_id", table_name="items")
    op.drop_table("items")
    op.drop_index("ix_decision_records_item_id", table_name="decision_records")
    op.drop_table("decision_records")
    op.drop_table("media_objects")
    op.drop_table("artifacts")
