"""LLM cost/usage columns and the local model registry.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "decision_records",
        sa.Column("llm_cost_usd", sa.REAL(), nullable=True),
    )
    op.add_column(
        "decision_records",
        sa.Column("llm_usage_json", sa.Text(), nullable=True),
    )
    op.create_table(
        "model_registry",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("version", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("kind", sa.Text(), nullable=False, server_default=sa.text("'ranker'")),
        sa.Column("license", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'missing'")),
        sa.Column("path", sa.Text(), nullable=True),
        sa.Column("checksum", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column(
            "heavy",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("updated_at", sa.Text(), nullable=False),
    )
    op.create_index("ix_model_registry_kind", "model_registry", ["kind"])


def downgrade() -> None:
    op.drop_index("ix_model_registry_kind", table_name="model_registry")
    op.drop_table("model_registry")
    op.drop_column("decision_records", "llm_usage_json")
    op.drop_column("decision_records", "llm_cost_usd")
