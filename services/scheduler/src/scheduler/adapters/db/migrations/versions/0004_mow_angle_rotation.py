"""Mowing angle rotation (ADR 0061).

* ``schedules.rotation_step_deg``: step rotation per schedule, null (off) for every existing
  schedule, so they all start exactly as before.
* ``schedule_runs.angle_deg``: the angle a started run sent; null for the runs so far.
* ``mow_angles``: the last angle per mower and area, where step rotation continues.
* ``mower_rotation``: per mower, the step the start sheet offers for manual starts.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Add the columns and the two tables."""
    op.add_column("schedules", sa.Column("rotation_step_deg", sa.Float(), nullable=True))
    op.add_column("schedule_runs", sa.Column("angle_deg", sa.Float(), nullable=True))
    op.create_table(
        "mow_angles",
        sa.Column("mower_id", sa.Uuid(), nullable=False),
        sa.Column("area_id", sa.String(128), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("angle_deg", sa.Float(), nullable=False),
        sa.Column("used_at", TS, nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("schedule_id", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("mower_id", "area_id", name="pk_mow_angles"),
    )
    op.create_table(
        "mower_rotation",
        sa.Column("mower_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("manual_step_deg", sa.Float(), nullable=True),
        sa.Column("updated_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("mower_id", name="pk_mower_rotation"),
    )


def downgrade() -> None:
    """Drop what ``upgrade`` added."""
    op.drop_table("mower_rotation")
    op.drop_table("mow_angles")
    op.drop_column("schedule_runs", "angle_deg")
    op.drop_column("schedules", "rotation_step_deg")
