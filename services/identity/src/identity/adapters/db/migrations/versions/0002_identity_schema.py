"""Identity schema: accounts, users, memberships, roles, assignments, sessions, API tokens,
signing keys, audit log (append-only), login attempts, permission catalogue; support grants and
invitations as schema only.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-09
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


def _account_fk(name: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["account_id"], ["accounts.id"], name=f"fk_{name}_account_id_accounts", ondelete="CASCADE"
    )


def upgrade() -> None:
    """Create the tables, indexes and the append-only trigger of the audit log."""
    op.create_table(
        "accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(80), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("plan", sa.String(64), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("delete_after", TS, nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_accounts"),
        sa.UniqueConstraint("slug", name="uq_accounts_slug"),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("username_norm", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("email", sa.String(254), nullable=True),
        sa.Column("email_norm", sa.String(254), nullable=True),
        sa.Column("owner_account_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "must_change_password", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("last_login_at", TS, nullable=True),
        sa.ForeignKeyConstraint(
            ["owner_account_id"],
            ["accounts.id"],
            name="fk_users_owner_account_id_accounts",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("username_norm", name="uq_users_username_norm"),
        sa.UniqueConstraint("email_norm", name="uq_users_email_norm"),
    )
    op.create_index("ix_users_owner_account_id", "users", ["owner_account_id"])
    op.create_table(
        "memberships",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("is_owner", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_admin", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        _account_fk("memberships"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_memberships_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_memberships"),
        sa.UniqueConstraint("account_id", "user_id", name="uq_memberships_account_id"),
    )
    op.create_index("ix_memberships_user_id", "memberships", ["user_id"])
    op.create_table(
        "roles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("permissions", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("description", sa.String(500), server_default="", nullable=False),
        _account_fk("roles"),
        sa.PrimaryKeyConstraint("id", name="pk_roles"),
        sa.UniqueConstraint("account_id", "key", name="uq_roles_account_id"),
    )
    op.create_table(
        "role_assignments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("subject_type", sa.String(20), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("role_id", sa.Uuid(), nullable=False),
        sa.Column("level_type", sa.String(20), nullable=False),
        sa.Column("level_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        _account_fk("role_assignments"),
        sa.ForeignKeyConstraint(
            ["role_id"], ["roles.id"], name="fk_role_assignments_role_id_roles", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_role_assignments"),
        sa.UniqueConstraint(
            "subject_type",
            "subject_id",
            "role_id",
            "level_type",
            "level_id",
            name="uq_role_assignments_subject_type",
        ),
    )
    op.create_index("ix_role_assignments_role_id", "role_assignments", ["role_id"])
    op.create_index(
        "ix_role_assignments_account_subject", "role_assignments", ["account_id", "subject_id"]
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("family_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("refresh_hash", sa.String(64), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("idle_expires_at", TS, nullable=False),
        sa.Column("absolute_expires_at", TS, nullable=False),
        sa.Column("last_used_at", TS, nullable=True),
        sa.Column("replaced_by", sa.Uuid(), nullable=True),
        sa.Column("revoked_at", TS, nullable=True),
        sa.Column("revoke_reason", sa.String(100), nullable=True),
        sa.Column("user_agent", sa.String(200), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        _account_fk("sessions"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_sessions_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sessions"),
        sa.UniqueConstraint("refresh_hash", name="uq_sessions_refresh_hash"),
    )
    op.create_index("ix_sessions_family_id", "sessions", ["family_id"])
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("prefix", sa.String(16), nullable=False),
        sa.Column("secret_hash", sa.String(64), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("grants", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=True),
        sa.Column("last_used_at", TS, nullable=True),
        sa.Column("revoked_at", TS, nullable=True),
        sa.Column("revoke_reason", sa.String(100), nullable=True),
        _account_fk("api_tokens"),
        sa.PrimaryKeyConstraint("id", name="pk_api_tokens"),
    )
    op.create_index("ix_api_tokens_account_id", "api_tokens", ["account_id"])
    op.create_index("ix_api_tokens_created_by", "api_tokens", ["created_by"])
    op.create_table(
        "signing_keys",
        sa.Column("kid", sa.String(64), nullable=False),
        sa.Column("alg", sa.String(16), nullable=False),
        sa.Column("public_jwk", postgresql.JSONB(), nullable=False),
        sa.Column("private_key_enc", sa.LargeBinary(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("activated_at", TS, nullable=True),
        sa.Column("retire_after", TS, nullable=True),
        sa.PrimaryKeyConstraint("kid", name="pk_signing_keys"),
    )
    op.create_index(
        "uq_signing_keys_one_active",
        "signing_keys",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("at", TS, nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("actor_subject", sa.String(64), nullable=True),
        sa.Column("actor_kind", sa.String(20), nullable=True),
        sa.Column("outcome", sa.String(20), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column("actor_name", sa.String(200), nullable=True),
        sa.Column("target_type", sa.String(32), nullable=True),
        sa.Column("target_id", sa.String(64), nullable=True),
        sa.Column("details", postgresql.JSONB(), nullable=False),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("correlation_id", sa.String(128), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_audit_log"),
    )
    op.create_index("ix_audit_log_account_id_id", "audit_log", ["account_id", "id"])
    op.execute(
        """
        CREATE FUNCTION audit_log_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log is append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_log_append_only
        BEFORE UPDATE OR DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION audit_log_append_only()
        """
    )
    op.create_table(
        "login_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("at", TS, nullable=False),
        sa.Column("username_norm", sa.String(64), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("reason", sa.String(100), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_login_attempts"),
    )
    op.create_index("ix_login_attempts_username_at", "login_attempts", ["username_norm", "at"])
    op.create_index("ix_login_attempts_ip_at", "login_attempts", ["ip", "at"])
    op.create_table(
        "permission_catalogue",
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column("module", sa.String(64), nullable=False),
        sa.Column("levels", postgresql.JSONB(), nullable=False),
        sa.Column("in_model", sa.Boolean(), nullable=False),
        sa.Column("first_seen_at", TS, nullable=False),
        sa.Column("last_seen_at", TS, nullable=False),
        sa.Column("implied_by", postgresql.JSONB(), nullable=False),
        sa.Column("users_only", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("retired", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.PrimaryKeyConstraint("key", name="pk_permission_catalogue"),
    )
    op.create_table(
        "support_grants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("support_user_id", sa.Uuid(), nullable=False),
        sa.Column("approved_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("revoked_at", TS, nullable=True),
        sa.Column("reason", sa.String(500), nullable=True),
        _account_fk("support_grants"),
        sa.PrimaryKeyConstraint("id", name="pk_support_grants"),
    )
    op.create_table(
        "invitations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("email", sa.String(254), nullable=True),
        sa.Column("role_id", sa.Uuid(), nullable=True),
        sa.Column("level_type", sa.String(20), nullable=True),
        sa.Column("level_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("accepted_at", TS, nullable=True),
        sa.Column("accepted_by", sa.Uuid(), nullable=True),
        _account_fk("invitations"),
        sa.ForeignKeyConstraint(
            ["role_id"], ["roles.id"], name="fk_invitations_role_id_roles", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_invitations"),
        sa.UniqueConstraint("token_hash", name="uq_invitations_token_hash"),
    )


def downgrade() -> None:
    """Drop everything this revision created (destroys all identity data)."""
    for table in (
        "invitations",
        "support_grants",
        "permission_catalogue",
        "login_attempts",
    ):
        op.drop_table(table)
    op.execute("DROP TRIGGER IF EXISTS audit_log_append_only ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_append_only()")
    for table in (
        "audit_log",
        "signing_keys",
        "api_tokens",
        "sessions",
        "role_assignments",
        "roles",
        "memberships",
        "users",
        "accounts",
    ):
        op.drop_table(table)
