"""Vision schema: proposals, their steps and images, cost accounting, user providers.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Create the tables and indexes."""
    op.create_table(
        "proposals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("chain_id", sa.Uuid(), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("mower_id", sa.Uuid(), nullable=False),
        sa.Column("task", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("requested_by_subject", sa.Uuid(), nullable=False),
        sa.Column("requested_by", postgresql.JSONB(), nullable=False),
        sa.Column("provider_model", sa.String(200), nullable=True),
        sa.Column("provider_family", sa.String(32), nullable=True),
        sa.Column("hosted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("situation", sa.String(32), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("assessment", postgresql.JSONB(), nullable=True),
        sa.Column("context", postgresql.JSONB(), nullable=False),
        sa.Column("image_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("detail", sa.String(500), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("analysed_at", TS, nullable=True),
        sa.Column("expires_at", TS, nullable=True),
        sa.Column("approved_by_subject", sa.Uuid(), nullable=True),
        sa.Column("approved_by", postgresql.JSONB(), nullable=True),
        sa.Column("approved_at", TS, nullable=True),
        sa.Column("decided_by", postgresql.JSONB(), nullable=True),
        sa.Column("decided_at", TS, nullable=True),
        sa.Column("cancel_requested_at", TS, nullable=True),
        sa.Column("started_at", TS, nullable=True),
        sa.Column("finished_at", TS, nullable=True),
        sa.Column("drive_session_id", sa.Uuid(), nullable=True),
        sa.Column("start_pose", postgresql.JSONB(), nullable=True),
        sa.Column("end_pose", postgresql.JSONB(), nullable=True),
        sa.Column("return_command_id", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_proposals")),
    )
    op.create_index(
        "ix_proposals_account_mower_created", "proposals", ["account_id", "mower_id", "created_at"]
    )
    op.create_index("ix_proposals_chain", "proposals", ["chain_id", "round"])
    op.create_index("ix_proposals_state_updated", "proposals", ["state", "updated_at"])
    op.create_index(
        "ux_proposals_open_mower",
        "proposals",
        ["mower_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('analysing', 'proposed', 'executing', 'verifying')"),
    )
    op.create_table(
        "proposal_steps",
        sa.Column("proposal_id", sa.Uuid(), nullable=False),
        sa.Column("index", sa.Integer(), nullable=False),
        sa.Column("linear", sa.Float(), nullable=False),
        sa.Column("angular", sa.Float(), nullable=False),
        sa.Column("seconds", sa.Float(), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("started_at", TS, nullable=True),
        sa.Column("ended_at", TS, nullable=True),
        sa.Column("frames", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("detail", sa.String(500), nullable=True),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["proposals.id"],
            name=op.f("fk_proposal_steps_proposal_id_proposals"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("proposal_id", "index", name=op.f("pk_proposal_steps")),
    )
    op.create_table(
        "proposal_images",
        sa.Column("proposal_id", sa.Uuid(), nullable=False),
        sa.Column("index", sa.Integer(), nullable=False),
        sa.Column("media_type", sa.String(32), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("delete_after", TS, nullable=False),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["proposals.id"],
            name=op.f("fk_proposal_images_proposal_id_proposals"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("proposal_id", "index", name=op.f("pk_proposal_images")),
    )
    op.create_index("ix_proposal_images_delete_after", "proposal_images", ["delete_after"])
    op.create_table(
        "provider_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("proposal_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("family", sa.String(32), nullable=False),
        sa.Column("hosted", sa.Boolean(), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("detail", sa.String(500), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_requests")),
    )
    op.create_index(
        "ix_provider_requests_user_created", "provider_requests", ["user_id", "kind", "created_at"]
    )
    op.create_index(
        "ix_provider_requests_account_created",
        "provider_requests",
        ["account_id", "kind", "created_at"],
    )
    op.create_table(
        "user_providers",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("api_base", sa.Text(), nullable=True),
        sa.Column("api_key_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("headers_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("header_names", postgresql.JSONB(), nullable=False),
        sa.Column("last_test", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("user_id", "account_id", name=op.f("pk_user_providers")),
    )


def downgrade() -> None:
    """Drop the tables."""
    op.drop_table("user_providers")
    op.drop_index("ix_provider_requests_account_created", table_name="provider_requests")
    op.drop_index("ix_provider_requests_user_created", table_name="provider_requests")
    op.drop_table("provider_requests")
    op.drop_index("ix_proposal_images_delete_after", table_name="proposal_images")
    op.drop_table("proposal_images")
    op.drop_table("proposal_steps")
    op.drop_index("ux_proposals_open_mower", table_name="proposals")
    op.drop_index("ix_proposals_state_updated", table_name="proposals")
    op.drop_index("ix_proposals_chain", table_name="proposals")
    op.drop_index("ix_proposals_account_mower_created", table_name="proposals")
    op.drop_table("proposals")
