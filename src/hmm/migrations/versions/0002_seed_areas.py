from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_AREAS: tuple[tuple[str, str, str], ...] = (
    ("backend", "Backend", "APIs, databases, server-side logic, integrations"),
    ("frontend", "Frontend", "web UI, client-side apps"),
    ("mobile", "Mobile", "iOS, Android, cross-platform apps"),
    ("ml_ai", "ML / AI", "machine learning, deep learning, LLM apps, CV/NLP"),
    ("data", "Data", "analytics, BI, data engineering, metrics and unit economics"),
    ("design", "Design", "UI/UX, graphics, branding, pitch visuals"),
    ("product", "Product", "product management, business analysis, scoping, pitching"),
    ("devops", "DevOps", "infrastructure, CI/CD, cloud, deployment"),
    ("security", "Security", "CTF, application and network security"),
    ("gamedev", "GameDev", "game design and development, engines, graphics"),
    ("hardware", "Hardware", "embedded, IoT, robotics, electronics"),
    ("qa", "QA", "testing, test automation, quality"),
)

areas = sa.table(
    "areas",
    sa.column("key", sa.Text()),
    sa.column("name", sa.Text()),
    sa.column("description", sa.Text()),
    sa.column("sort", sa.Integer()),
)


def upgrade() -> None:
    op.bulk_insert(
        areas,
        [
            {"key": key, "name": name, "description": description, "sort": index * 10}
            for index, (key, name, description) in enumerate(SEED_AREAS, start=1)
        ],
    )


def downgrade() -> None:
    op.execute(areas.delete().where(areas.c.key.in_([key for key, _, _ in SEED_AREAS])))
