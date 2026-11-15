from collections.abc import Sequence

from alembic import op

revision: str = "0025"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS: tuple[tuple[str, str], ...] = (
    ("audit_log", "before"),
    ("audit_log", "after"),
    ("events", "location"),
    ("events", "prize"),
    ("flags", "penalty"),
    ("formation_runs", "output"),
    ("formation_runs", "validation_errors"),
    ("github_snapshots", "profile"),
    ("github_snapshots", "repos"),
    ("github_snapshots", "languages"),
    ("github_snapshots", "stats"),
    ("projects", "recruiting"),
    ("questions", "visible_if"),
    ("quiz_sections", "visible_if"),
    ("recommendation_sets", "enrolled"),
    ("recommendation_sets", "others"),
)


def upgrade() -> None:
    for table, column in COLUMNS:
        op.execute(f"UPDATE {table} SET {column} = NULL WHERE jsonb_typeof({column}) = 'null'")


def downgrade() -> None:
    pass
