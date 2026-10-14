"""SQLAlchemy table definitions. Alembic compares migrations against :data:`metadata`."""

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
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
"""Metadata of all tables of the service (add tables with ``Table(..., metadata)``)."""

TS = DateTime(timezone=True)

OPEN_STATES_SQL = "state IN ('analysing', 'proposed', 'executing', 'verifying')"

# One round of AI assist for one mower: the audit of what was asked, answered and decided.
proposals = Table(
    "proposals",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("chain_id", Uuid, nullable=False),
    Column("round", Integer, nullable=False),
    Column("parent_id", Uuid),
    Column("account_id", Uuid, nullable=False),
    Column("mower_id", Uuid, nullable=False),
    Column("task", String(16), nullable=False),
    Column("state", String(16), nullable=False),
    Column("requested_by_subject", Uuid, nullable=False),
    Column("requested_by", JSONB, nullable=False),
    Column("provider_model", String(200)),
    Column("provider_family", String(32)),
    Column("hosted", Boolean, nullable=False, server_default=text("false")),
    Column("situation", String(32)),
    Column("confidence", Float),
    Column("assessment", JSONB),
    Column("context", JSONB, nullable=False),
    Column("image_count", Integer, nullable=False, server_default=text("0")),
    Column("light_used", Boolean, nullable=False, server_default=text("false")),
    Column("stereo", Boolean, nullable=False, server_default=text("false")),
    # What each image was (camera, side, capture time), in the order the model saw them.
    Column("frames", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("trigger", String(16), nullable=False, server_default=text("'manual'")),
    Column("trigger_error_code", String(64)),
    Column("detail", String(500)),
    Column("created_at", TS, nullable=False),
    Column("updated_at", TS, nullable=False),
    Column("analysed_at", TS),
    Column("expires_at", TS),
    Column("approved_by_subject", Uuid),
    Column("approved_by", JSONB),
    Column("approved_at", TS),
    Column("decided_by", JSONB),
    Column("decided_at", TS),
    Column("cancel_requested_at", TS),
    Column("started_at", TS),
    Column("finished_at", TS),
    Column("drive_session_id", Uuid),
    Column("start_pose", JSONB),
    Column("end_pose", JSONB),
    Column("return_command_id", Uuid),
    Index("ix_proposals_account_mower_created", "account_id", "mower_id", "created_at"),
    Index("ix_proposals_chain", "chain_id", "round"),
    Index("ix_proposals_state_updated", "state", "updated_at"),
    Index("ix_proposals_mower_trigger_created", "mower_id", "trigger", "created_at"),
    # At most one open proposal per mower (a second request answers 409).
    Index(
        "ux_proposals_open_mower",
        "mower_id",
        unique=True,
        postgresql_where=text(OPEN_STATES_SQL),
    ),
)

# The audit of every drive step of an approved proposal.
proposal_steps = Table(
    "proposal_steps",
    metadata,
    Column("proposal_id", Uuid, ForeignKey("proposals.id", ondelete="CASCADE"), primary_key=True),
    Column("index", Integer, primary_key=True),
    Column("linear", Float, nullable=False),
    Column("angular", Float, nullable=False),
    Column("seconds", Float, nullable=False),
    Column("outcome", String(16), nullable=False),
    Column("started_at", TS),
    Column("ended_at", TS),
    Column("frames", Integer, nullable=False, server_default=text("0")),
    Column("detail", String(500)),
)

# Images the model saw; deleted after delete_after (the proposal TTL, or the keep days).
proposal_images = Table(
    "proposal_images",
    metadata,
    Column("proposal_id", Uuid, ForeignKey("proposals.id", ondelete="CASCADE"), primary_key=True),
    Column("index", Integer, primary_key=True),
    Column("media_type", String(32), nullable=False),
    Column("source", String(16), nullable=False),
    Column("data", LargeBinary, nullable=False),
    Column("delete_after", TS, nullable=False),
    Index("ix_proposal_images_delete_after", "delete_after"),
)

# Cost accounting: one row per model call (analyses and provider tests); daily caps count it.
provider_requests = Table(
    "provider_requests",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("account_id", Uuid, nullable=False),
    Column("user_id", Uuid, nullable=False),
    Column("proposal_id", Uuid),
    Column("kind", String(16), nullable=False),
    Column("model", String(200), nullable=False),
    Column("family", String(32), nullable=False),
    Column("hosted", Boolean, nullable=False),
    Column("outcome", String(16), nullable=False),
    Column("input_tokens", Integer),
    Column("output_tokens", Integer),
    Column("cost_usd", Float),
    Column("latency_ms", Integer),
    Column("detail", String(500)),
    Column("created_at", TS, nullable=False),
    Index("ix_provider_requests_user_created", "user_id", "kind", "created_at"),
    Index("ix_provider_requests_account_created", "account_id", "kind", "created_at"),
)

# A user's own vision model; the key and the extra headers are AES-GCM ciphertexts.
user_providers = Table(
    "user_providers",
    metadata,
    Column("user_id", Uuid, primary_key=True),
    Column("account_id", Uuid, primary_key=True),
    Column("model", String(200), nullable=False),
    Column("enabled", Boolean, nullable=False),
    Column("api_base", Text),
    Column("api_key_ciphertext", LargeBinary),
    Column("headers_ciphertext", LargeBinary),
    Column("header_names", JSONB, nullable=False),
    Column("last_test", JSONB),
    Column("created_at", TS, nullable=False),
    Column("updated_at", TS, nullable=False),
)

# A user's AI assist preferences (absent: the defaults): the stereo frame preference (revision
# 0004) and the opt-in to automatic analyses with the principal kind the analysis acts as
# (revision 0005, ADR 0035). Each store method writes only its own columns, so the other
# preferences keep their values.
user_preferences = Table(
    "user_preferences",
    metadata,
    Column("user_id", Uuid, primary_key=True),
    Column("account_id", Uuid, primary_key=True),
    Column("stereo_frames", Boolean, nullable=False, server_default=text("true")),
    Column("auto_analysis", Boolean, nullable=False, server_default=text("false")),
    Column("principal_kind", String(16), nullable=False, server_default=text("'user'")),
    Column("updated_at", TS, nullable=False),
    Index("ix_user_preferences_account", "account_id"),
)

# The newest recoverable error seen per mower (edge detection of the automatic analysis: only
# entering an error starts one, not every state while it lasts).
auto_watch = Table(
    "auto_watch",
    metadata,
    Column("mower_id", Uuid, primary_key=True),
    Column("account_id", Uuid, nullable=False),
    Column("error_code", String(64)),
    Column("changed_at", TS, nullable=False),
)
