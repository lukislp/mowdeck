"""Custom names of map areas per mower (area renaming in MowDeck).

Additive: one new table, so a running older Fleet keeps working during the rollout. The rows
go with their mower (``ON DELETE CASCADE``).

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``mower_area_names``."""
    op.create_table(
        "mower_area_names",
        sa.Column("mower_id", sa.Uuid(), nullable=False),
        sa.Column("area_id", sa.String(128), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["mower_id"],
            ["mowers.id"],
            name="fk_mower_area_names_mower_id_mowers",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("mower_id", "area_id", name="pk_mower_area_names"),
    )


def downgrade() -> None:
    """Drop the table again."""
    op.drop_table("mower_area_names")
