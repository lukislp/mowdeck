"""Passkeys (ADR 0050): WebAuthn credentials of users, the challenges of registration and login
ceremonies, and the one-time codes that hand a login in the system browser to the native app.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def _user_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["user_id"], ["users.id"], name=f"fk_{table}_user_id_users", ondelete="CASCADE"
    )


def upgrade() -> None:
    """Create the three tables."""
    op.create_table(
        "passkeys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("credential_id", sa.LargeBinary(), nullable=False),
        sa.Column("public_key", sa.LargeBinary(), nullable=False),
        sa.Column("sign_count", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("transports", postgresql.JSONB(), nullable=False),
        sa.Column("aaguid", sa.String(36), nullable=True),
        sa.Column("backup_eligible", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("backed_up", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("last_used_at", TS, nullable=True),
        sa.Column("created_user_agent", sa.String(200), nullable=True),
        _user_fk("passkeys"),
        sa.PrimaryKeyConstraint("id", name="pk_passkeys"),
        sa.UniqueConstraint("credential_id", name="uq_passkeys_credential_id"),
    )
    op.create_index("ix_passkeys_user_id", "passkeys", ["user_id"])
    op.create_table(
        "webauthn_challenges",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("challenge", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        _user_fk("webauthn_challenges"),
        sa.PrimaryKeyConstraint("id", name="pk_webauthn_challenges"),
    )
    op.create_index("ix_webauthn_challenges_expires_at", "webauthn_challenges", ["expires_at"])
    op.create_index("ix_webauthn_challenges_user_id", "webauthn_challenges", ["user_id"])
    op.create_table(
        "app_login_codes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("code_challenge", sa.String(64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("source_session", sa.Uuid(), nullable=True),
        _user_fk("app_login_codes"),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name="fk_app_login_codes_account_id_accounts",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_app_login_codes"),
        sa.UniqueConstraint("code_hash", name="uq_app_login_codes_code_hash"),
    )
    op.create_index("ix_app_login_codes_expires_at", "app_login_codes", ["expires_at"])
    op.create_index("ix_app_login_codes_user_id", "app_login_codes", ["user_id"])


def downgrade() -> None:
    """Drop the tables (every registered passkey is lost; passwords stay)."""
    for table in ("app_login_codes", "webauthn_challenges", "passkeys"):
        op.drop_table(table)
