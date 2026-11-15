from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "event_formations",
        sa.Column("reg_cutoff_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "event_formations",
        sa.Column("late_round_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("event_formations", "late_round_at")
    op.drop_column("event_formations", "reg_cutoff_at")
