"""Demo sandboxes: one account per visitor of the public demo (ADR 0076).

``demo_sandboxes``: the pool and the visitors' sandboxes with their purge progress. Only a demo
installation writes rows; everywhere else the table stays empty.

The append-only trigger of ``audit_log`` still refuses every ``UPDATE`` and ``DELETE``, except a
``DELETE`` in a transaction that set ``mowdeck.audit_purge`` to ``on`` (``set_config`` with
``is_local``), which only the purge of a demo sandbox does.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the table."""
    op.create_table(
        "demo_sandboxes",
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("purge_reason", sa.String(length=32), nullable=True),
        sa.Column("purged", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.PrimaryKeyConstraint("account_id", name=op.f("pk_demo_sandboxes")),
    )
    op.create_index(op.f("ix_demo_sandboxes_state"), "demo_sandboxes", ["state"], unique=False)
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_log_append_only() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE'
                AND coalesce(current_setting('mowdeck.audit_purge', true), '') = 'on' THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'audit_log is append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )


def downgrade() -> None:
    """The strict trigger function of revision 0002; drop the table."""
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_log_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log is append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.drop_index(op.f("ix_demo_sandboxes_state"), table_name="demo_sandboxes")
    op.drop_table("demo_sandboxes")
