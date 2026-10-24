"""SQLAlchemy table definitions. Alembic compares migrations against :data:`metadata`.

Column names equal the field names of the entities in ``identity.domain.models``.
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)
"""Metadata of all tables of the service."""

TS = DateTime(timezone=True)


def _account_fk() -> ForeignKey:
    return ForeignKey("accounts.id", ondelete="CASCADE")


accounts = Table(
    "accounts",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("name", String(200), nullable=False),
    Column("slug", String(80), nullable=False, unique=True),
    Column("kind", String(20), nullable=False),
    Column("status", String(20), nullable=False),
    Column("plan", String(64), nullable=False),
    Column("created_at", TS, nullable=False),
    Column("delete_after", TS),
)

users = Table(
    "users",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("username", String(64), nullable=False),
    Column("username_norm", String(64), nullable=False, unique=True),
    Column("display_name", String(200), nullable=False),
    Column("kind", String(20), nullable=False),
    Column("status", String(20), nullable=False),
    Column("created_at", TS, nullable=False),
    Column("password_hash", Text),
    Column("email", String(254)),
    Column("email_norm", String(254), unique=True),
    Column("owner_account_id", Uuid, _account_fk(), index=True),
    Column("created_by", Uuid),
    Column("must_change_password", Boolean, nullable=False, server_default=text("false")),
    Column("last_login_at", TS),
)

memberships = Table(
    "memberships",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("account_id", Uuid, _account_fk(), nullable=False),
    Column("user_id", Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("status", String(20), nullable=False),
    Column("created_at", TS, nullable=False),
    Column("is_owner", Boolean, nullable=False, server_default=text("false")),
    Column("is_admin", Boolean, nullable=False, server_default=text("false")),
    UniqueConstraint("account_id", "user_id"),
)

roles = Table(
    "roles",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("account_id", Uuid, _account_fk(), nullable=False),
    Column("key", String(64), nullable=False),
    Column("name", String(100), nullable=False),
    Column("kind", String(20), nullable=False),
    Column("permissions", JSONB, nullable=False),
    Column("created_at", TS, nullable=False),
    Column("updated_at", TS, nullable=False),
    Column("description", String(500), nullable=False, server_default=""),
    UniqueConstraint("account_id", "key"),
)

role_assignments = Table(
    "role_assignments",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("account_id", Uuid, _account_fk(), nullable=False),
    Column("subject_type", String(20), nullable=False),
    Column("subject_id", Uuid, nullable=False),
    Column(
        "role_id", Uuid, ForeignKey("roles.id", ondelete="RESTRICT"), nullable=False, index=True
    ),
    Column("level_type", String(20), nullable=False),
    Column("level_id", Uuid, nullable=False),
    Column("created_at", TS, nullable=False),
    Column("created_by", Uuid),
    UniqueConstraint("subject_type", "subject_id", "role_id", "level_type", "level_id"),
    Index("ix_role_assignments_account_subject", "account_id", "subject_id"),
)

sessions = Table(
    "sessions",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("family_id", Uuid, nullable=False, index=True),
    Column("user_id", Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("account_id", Uuid, _account_fk(), nullable=False),
    Column("refresh_hash", String(64), nullable=False, unique=True),
    Column("created_at", TS, nullable=False),
    Column("idle_expires_at", TS, nullable=False),
    Column("absolute_expires_at", TS, nullable=False),
    Column("last_used_at", TS),
    Column("replaced_by", Uuid),
    Column("revoked_at", TS),
    Column("revoke_reason", String(100)),
    Column("user_agent", String(200)),
    Column("ip", String(64)),
)

api_tokens = Table(
    "api_tokens",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("account_id", Uuid, _account_fk(), nullable=False, index=True),
    Column("name", String(100), nullable=False),
    Column("prefix", String(16), nullable=False),
    Column("secret_hash", String(64), nullable=False),
    Column("created_by", Uuid, nullable=False, index=True),
    Column("grants", JSONB, nullable=False),
    Column("created_at", TS, nullable=False),
    Column("expires_at", TS),
    Column("last_used_at", TS),
    Column("revoked_at", TS),
    Column("revoke_reason", String(100)),
)

signing_keys = Table(
    "signing_keys",
    metadata,
    Column("kid", String(64), primary_key=True),
    Column("alg", String(16), nullable=False),
    Column("public_jwk", JSONB, nullable=False),
    Column("private_key_enc", LargeBinary, nullable=False),
    Column("status", String(16), nullable=False),
    Column("created_at", TS, nullable=False),
    Column("activated_at", TS),
    Column("retire_after", TS),
    Index(
        "uq_signing_keys_one_active",
        "status",
        unique=True,
        postgresql_where=text("status = 'active'"),
    ),
)

audit_log = Table(
    "audit_log",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("at", TS, nullable=False),
    Column("action", String(64), nullable=False),
    Column("actor_subject", String(64)),
    Column("actor_kind", String(20)),
    Column("outcome", String(20), nullable=False),
    Column("account_id", Uuid),
    Column("actor_name", String(200)),
    Column("target_type", String(32)),
    Column("target_id", String(64)),
    Column("details", JSONB, nullable=False),
    Column("ip", String(64)),
    Column("correlation_id", String(128)),
    Index("ix_audit_log_account_id_id", "account_id", "id"),
)

login_attempts = Table(
    "login_attempts",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("at", TS, nullable=False),
    Column("username_norm", String(64), nullable=False),
    Column("success", Boolean, nullable=False),
    Column("ip", String(64)),
    Column("reason", String(100)),
    Index("ix_login_attempts_username_at", "username_norm", "at"),
    Index("ix_login_attempts_ip_at", "ip", "at"),
)

permission_catalogue = Table(
    "permission_catalogue",
    metadata,
    Column("key", String(128), primary_key=True),
    Column("description", String(500), nullable=False),
    Column("module", String(64), nullable=False),
    Column("levels", JSONB, nullable=False),
    Column("in_model", Boolean, nullable=False),
    Column("first_seen_at", TS, nullable=False),
    Column("last_seen_at", TS, nullable=False),
    Column("implied_by", JSONB, nullable=False),
    Column("users_only", Boolean, nullable=False, server_default=text("false")),
    Column("retired", Boolean, nullable=False, server_default=text("false")),
)

# Passkeys (ADR 0050): credentials, ceremony challenges; the app login hand-off codes.
passkeys = Table(
    "passkeys",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("user_id", Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("credential_id", LargeBinary, nullable=False, unique=True),
    Column("public_key", LargeBinary, nullable=False),
    Column("sign_count", BigInteger, nullable=False),
    Column("name", String(64), nullable=False),
    Column("created_at", TS, nullable=False),
    Column("transports", JSONB, nullable=False),
    Column("aaguid", String(36)),
    Column("backup_eligible", Boolean, nullable=False, server_default=text("false")),
    Column("backed_up", Boolean, nullable=False, server_default=text("false")),
    Column("last_used_at", TS),
    Column("created_user_agent", String(200)),
)

webauthn_challenges = Table(
    "webauthn_challenges",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("purpose", String(16), nullable=False),
    Column("challenge", LargeBinary, nullable=False),
    Column("created_at", TS, nullable=False),
    Column("expires_at", TS, nullable=False, index=True),
    Column("user_id", Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True),
)

app_login_codes = Table(
    "app_login_codes",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("code_hash", String(64), nullable=False, unique=True),
    Column("code_challenge", String(64), nullable=False),
    Column("user_id", Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("account_id", Uuid, _account_fk(), nullable=False),
    Column("created_at", TS, nullable=False),
    Column("expires_at", TS, nullable=False, index=True),
    Column("source_session", Uuid),
)

# Schema only in Phase 1 (design section 5): support access and invitations.
support_grants = Table(
    "support_grants",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("account_id", Uuid, _account_fk(), nullable=False),
    Column("support_user_id", Uuid, nullable=False),
    Column("approved_by", Uuid, nullable=False),
    Column("created_at", TS, nullable=False),
    Column("expires_at", TS, nullable=False),
    Column("revoked_at", TS),
    Column("reason", String(500)),
)

invitations = Table(
    "invitations",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("account_id", Uuid, _account_fk(), nullable=False),
    Column("token_hash", String(64), nullable=False, unique=True),
    Column("email", String(254)),
    Column("role_id", Uuid, ForeignKey("roles.id", ondelete="CASCADE")),
    Column("level_type", String(20)),
    Column("level_id", Uuid),
    Column("created_by", Uuid, nullable=False),
    Column("created_at", TS, nullable=False),
    Column("expires_at", TS, nullable=False),
    Column("accepted_at", TS),
    Column("accepted_by", Uuid),
)
