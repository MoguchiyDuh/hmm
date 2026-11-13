from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS = (
    "'formation_undersized', 'formation_failed', 'team_below_min', 'event_dates_changed', "
    "'forced_lead', 'lead_below_threshold', 'result_overdue', 'import_review', "
    "'event_cancelled_with_teams', 'team_size_conflict', 'suspicious_activity', "
    "{extra}'review_reported', 'banned_member_in_active_team', 'algo_failure', "
    "'job_failing', 'parser_down'"
)


def replace_type_check(kinds: str) -> None:
    op.drop_constraint(op.f("ck_admin_alerts_type_enum"), "admin_alerts", type_="check")
    op.create_check_constraint(
        op.f("ck_admin_alerts_type_enum"), "admin_alerts", f"type IN ({kinds})"
    )


def upgrade() -> None:
    op.execute("DELETE FROM admin_alerts WHERE type = 'restriction_violation'")
    replace_type_check(KINDS.format(extra=""))


def downgrade() -> None:
    replace_type_check(KINDS.format(extra="'restriction_violation', "))
