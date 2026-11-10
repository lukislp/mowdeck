# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Explicit consent before data goes to a cloud AI provider (ADR 0081): when a user agreed and
for which destination (provider family, model and endpoint), in ``user_preferences``. Nobody has a
consent after this revision, so everyone is asked on first use.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable consent columns (all null: no consent)."""
    op.add_column(
        "user_preferences", sa.Column("consent_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("user_preferences", sa.Column("consent_kind", sa.String(16), nullable=True))
    op.add_column("user_preferences", sa.Column("consent_provider", sa.String(100), nullable=True))
    op.add_column("user_preferences", sa.Column("consent_family", sa.String(64), nullable=True))
    op.add_column("user_preferences", sa.Column("consent_model", sa.String(200), nullable=True))
    op.add_column("user_preferences", sa.Column("consent_endpoint", sa.String(300), nullable=True))


def downgrade() -> None:
    """Drop what :func:`upgrade` added."""
    op.drop_column("user_preferences", "consent_endpoint")
    op.drop_column("user_preferences", "consent_model")
    op.drop_column("user_preferences", "consent_family")
    op.drop_column("user_preferences", "consent_provider")
    op.drop_column("user_preferences", "consent_kind")
    op.drop_column("user_preferences", "consent_at")
