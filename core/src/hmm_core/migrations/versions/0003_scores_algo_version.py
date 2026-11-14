from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("area_score_events", "algo_version", existing_type=sa.Text(), nullable=True)
    op.create_table(
        "onboarding_baselines",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("area_scores", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("discrepancies", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_onboarding_baselines_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_onboarding_baselines")),
    )


def downgrade() -> None:
    op.drop_table("onboarding_baselines")
    op.execute("UPDATE area_score_events SET algo_version = 'admin' WHERE algo_version IS NULL")
    op.alter_column("area_score_events", "algo_version", existing_type=sa.Text(), nullable=False)
