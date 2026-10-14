"""Automatic AI analyses (ADR 0035): users who receive ``vision_proposal`` also receive the new
kind ``vision_auto_proposal``.

Data only: stored preferences that enable ``vision_proposal`` get ``vision_auto_proposal``
added to ``enabled_kinds`` (a user who opted in to automatic analyses would otherwise never hear
about the proposal), so nobody loses a notification they asked for. Users without stored
preferences get it from the defaults. The downgrade removes the kind again.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``vision_auto_proposal`` where ``vision_proposal`` is enabled."""
    op.execute(
        sa.text(
            "UPDATE preferences SET enabled_kinds = enabled_kinds || '[\"vision_auto_proposal\"]'"
            "::jsonb WHERE enabled_kinds @> '[\"vision_proposal\"]'::jsonb"
            " AND NOT enabled_kinds @> '[\"vision_auto_proposal\"]'::jsonb"
        )
    )


def downgrade() -> None:
    """Remove ``vision_auto_proposal`` from every stored preference."""
    op.execute(
        sa.text(
            "UPDATE preferences SET enabled_kinds = enabled_kinds - 'vision_auto_proposal'"
            " WHERE enabled_kinds @> '[\"vision_auto_proposal\"]'::jsonb"
        )
    )
