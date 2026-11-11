# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""A deleted user's audit rows lose their personal fields (ADR 0082, "delete me").

Revision 0011 gave the append-only audit log one sanctioned change, the scrub of an erased
account: ``actor_name`` and ``ip`` become NULL and ``details`` loses keys. A member who deletes
themselves leaves an account that goes on, and some rows about them were written by somebody
else (an admin created or disabled them): there the name and the address are the admin's and
must stay. So the trigger's rule becomes "``actor_name`` and ``ip`` each stay as they are or
become NULL"; still nothing can be added or changed, only removed, and only while
``mowdeck.audit_scrub`` is on.

``audit_log_scrub_user(user_id)`` is the second (and only other) function that sets the flag.
It touches the rows the user acted in or is the target of: ``details`` keeps the keys of
revision 0011's list, and the name and address go unless another actor is named. Idempotent.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-11
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
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
"""The ``details`` keys that stay: the list of revision 0011 (frozen here too; a test compares
both with ``AUDIT_DETAILS_KEPT``)."""

_TRIGGER = """
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
            AND {personal}
            AND OLD.details @> NEW.details THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'audit_log is append-only';
    END;
    $$ LANGUAGE plpgsql
"""
_REMOVED_OR_KEPT = (
    "(NEW.actor_name IS NULL OR NEW.actor_name IS NOT DISTINCT FROM OLD.actor_name)"
    "\n            AND (NEW.ip IS NULL OR NEW.ip IS NOT DISTINCT FROM OLD.ip)"
)
_REMOVED = "NEW.actor_name IS NULL\n            AND NEW.ip IS NULL"


def upgrade() -> None:
    """Let the scrub keep another actor's name and address; add the scrub of one user."""
    op.execute(_TRIGGER.format(personal=_REMOVED_OR_KEPT))
    op.execute(
        f"""
        CREATE FUNCTION audit_log_scrub_user(p_user_id uuid) RETURNS integer AS $$
        DECLARE
            changed integer;
            own text := 'user:' || p_user_id::text;
        BEGIN
            PERFORM set_config('mowdeck.audit_scrub', 'on', true);
            UPDATE audit_log AS a
            SET actor_name = CASE
                    WHEN a.actor_subject IS NOT NULL AND a.actor_subject <> own
                    THEN a.actor_name END,
                ip = CASE
                    WHEN a.actor_subject IS NOT NULL AND a.actor_subject <> own
                    THEN a.ip END,
                details = coalesce(
                    (SELECT jsonb_object_agg(e.k, e.v)
                       FROM jsonb_each(a.details) AS e(k, v)
                      WHERE e.k = ANY ({KEPT})),
                    '{{}}'::jsonb)
            WHERE (a.actor_subject = own
                   OR (a.target_type = 'user' AND a.target_id = p_user_id::text))
              AND (((a.actor_subject IS NULL OR a.actor_subject = own)
                    AND (a.actor_name IS NOT NULL OR a.ip IS NOT NULL))
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
    """Drop the user scrub; the trigger rule of revision 0011."""
    op.execute("DROP FUNCTION IF EXISTS audit_log_scrub_user(uuid)")
    op.execute(_TRIGGER.format(personal=_REMOVED))
