# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Account erasure (ADR 0082): the purge record and the sanctioned audit scrub.

``account_erasures``: one row per account whose grace period ended (state, which services
confirmed, retries). No foreign key and no personal data; the row outlives the account.

The audit log stays append-only. ``audit_log_append_only()`` (revision 0010: a ``DELETE`` passes
while ``mowdeck.audit_purge`` is on, the demo purge only) now lets exactly one more thing pass:
an ``UPDATE`` while ``mowdeck.audit_scrub`` is on that changes nothing but the personal columns
and only removes data (``actor_name`` and ``ip`` become NULL, ``details`` ends up a subset of
what it was). Every other column must be equal. Nothing but the function ``audit_log_scrub``
sets the flag, and it clears the flag again. The keys of ``details`` that stay are the same list
as ``AUDIT_DETAILS_KEPT`` in ``identity.domain.erasure`` (a test compares both).

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KEPT = (
    "ARRAY["
    "'aaguid', 'by_admin', 'counts', 'delete_after', 'deletes', 'expires_at', "
    "'former_owners', 'grants', 'jti', 'key', 'kind', 'level', 'method', "
    "'new_login_alerts', 'owner', 'permissions', 'prefix', 'provider', 'reason', "
    "'replaces', 'revoked_at', 'role', 'scope', 'services', 'session', 'sessions', "
    "'sessions_ended', 'subuser', 'writes'"
    "]"
)
"""The ``details`` keys that stay (frozen here: a migration never follows later code changes)."""


def upgrade() -> None:
    """Create the table, widen the trigger by the scrub, add the scrub function."""
    op.create_table(
        "account_erasures",
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("required", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("confirmed", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("counts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("account_id", name=op.f("pk_account_erasures")),
    )
    op.create_index(op.f("ix_account_erasures_state"), "account_erasures", ["state"], unique=False)
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_log_append_only() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE'
                AND coalesce(current_setting('mowdeck.audit_purge', true), '') = 'on' THEN
                RETURN OLD;
            END IF;
            IF TG_OP = 'UPDATE'
                AND coalesce(current_setting('mowdeck.audit_scrub', true), '') = 'on'
                AND NEW.id = OLD.id
                AND NEW.at = OLD.at
                AND NEW.action = OLD.action
                AND NEW.actor_subject IS NOT DISTINCT FROM OLD.actor_subject
                AND NEW.actor_kind IS NOT DISTINCT FROM OLD.actor_kind
                AND NEW.outcome = OLD.outcome
                AND NEW.account_id IS NOT DISTINCT FROM OLD.account_id
                AND NEW.target_type IS NOT DISTINCT FROM OLD.target_type
                AND NEW.target_id IS NOT DISTINCT FROM OLD.target_id
                AND NEW.correlation_id IS NOT DISTINCT FROM OLD.correlation_id
                AND NEW.actor_name IS NULL
                AND NEW.ip IS NULL
                AND OLD.details @> NEW.details THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'audit_log is append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        f"""
        CREATE FUNCTION audit_log_scrub(p_account_id uuid) RETURNS integer AS $$
        DECLARE
            changed integer;
        BEGIN
            PERFORM set_config('mowdeck.audit_scrub', 'on', true);
            UPDATE audit_log AS a
            SET actor_name = NULL,
                ip = NULL,
                details = coalesce(
                    (SELECT jsonb_object_agg(e.k, e.v)
                       FROM jsonb_each(a.details) AS e(k, v)
                      WHERE e.k = ANY ({KEPT})),
                    '{{}}'::jsonb)
            WHERE a.account_id = p_account_id
              AND (a.actor_name IS NOT NULL
                   OR a.ip IS NOT NULL
                   OR EXISTS (SELECT 1 FROM jsonb_object_keys(a.details) AS d(k)
                               WHERE d.k <> ALL ({KEPT})));
            GET DIAGNOSTICS changed = ROW_COUNT;
            PERFORM set_config('mowdeck.audit_scrub', '', true);
            RETURN changed;
        END;
        $$ LANGUAGE plpgsql
        """  # noqa: S608 - KEPT is a constant list of this file
    )


def downgrade() -> None:
    """The trigger function of revision 0010; drop the function and the table."""
    op.execute("DROP FUNCTION IF EXISTS audit_log_scrub(uuid)")
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
    op.drop_index(op.f("ix_account_erasures_state"), table_name="account_erasures")
    op.drop_table("account_erasures")
