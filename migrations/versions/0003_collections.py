"""Collections and ordered collection items.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "collections",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("source_ref_json", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("owner", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("context_claims_json", sa.Text(), nullable=False),
        sa.Column(
            "tag_basis",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'album_first'"),
        ),
        sa.Column("warnings_json", sa.Text(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("created_at", sa.Text(), nullable=False),
    )
    op.create_index("ix_collections_source_ref", "collections", ["source_ref_json"])

    op.create_table(
        "collection_items",
        sa.Column("collection_id", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("candidate_json", sa.Text(), nullable=False),
        sa.Column("item_id", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'planned'"),
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["collection_id"], ["collections.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["item_id"], ["items.id"]),
        sa.PrimaryKeyConstraint("collection_id", "position"),
    )
    op.create_index(
        "ix_collection_items_status",
        "collection_items",
        ["collection_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_collection_items_status", table_name="collection_items")
    op.drop_table("collection_items")
    op.drop_index("ix_collections_source_ref", table_name="collections")
    op.drop_table("collections")
