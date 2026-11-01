"""The demo purge may delete an account's audit rows (ADR 0076).

The append-only trigger of ``audit_log`` still refuses every ``UPDATE`` and every ``DELETE``,
except a ``DELETE`` in a transaction that set ``mowdeck.audit_purge`` to ``on`` (``set_config``
with ``is_local``), which only the demo purge of a sandbox account does. No table changes; a
running older Fleet keeps working during the rollout.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-01
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Let a ``DELETE`` pass while ``mowdeck.audit_purge`` is ``on``."""
    op.execute(
        """
        CREATE OR REPLACE FUNCTION fleet_audit_log_append_only() RETURNS trigger AS $$
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
    """The strict trigger function of revision 0002."""
    op.execute(
        """
        CREATE OR REPLACE FUNCTION fleet_audit_log_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log is append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
