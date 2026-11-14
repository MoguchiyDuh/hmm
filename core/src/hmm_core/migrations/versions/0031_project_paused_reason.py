from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("paused_reason", sa.Text(), nullable=True))
    op.create_check_constraint(
        "paused_reason",
        "projects",
        "paused_reason IS NULL OR paused_reason IN ('manual', 'idle')",
    )


def downgrade() -> None:
    op.drop_constraint("paused_reason", "projects", type_="check")
    op.drop_column("projects", "paused_reason")
