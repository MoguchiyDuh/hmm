from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENUMS = (
    (
        "event_formations",
        "phase",
        "'open', 'forming', 'review', 'locked', 'closed'",
        "'open', 'forming', 'formed', 'closed'",
    ),
    (
        "projects",
        "team_status",
        "'forming', 'review', 'locked', 'active', 'finished', 'withdrawn', "
        "'disbanded', 'cancelled'",
        "'formed', 'active', 'finished', 'withdrawn', 'disbanded', 'cancelled'",
    ),
    (
        "projects",
        "origin",
        "'standalone', 'claim', 'formation', 'late_claim', 'admin'",
        "'standalone', 'formation', 'admin'",
    ),
    (
        "project_members",
        "joined_via",
        "'create', 'invite', 'claim', 'core_invite', 'join_request', 'formation', "
        "'fill_invite', 'reserve_add', 'reserve_offer', 'swap', 'merge', 'admin'",
        "'create', 'invite', 'join_request', 'formation', 'refill', 'admin'",
    ),
    (
        "project_members",
        "left_reason",
        "'declined', 'lapsed', 'removed', 'left', 'swapped', 'merged', "
        "'disbanded', 'banned', 'admin'",
        "'declined', 'lapsed', 'removed', 'left', 'disbanded', 'banned', 'admin'",
    ),
    ("invites", "kind", "'core', 'fill', 'project'", "'party', 'team'"),
)

PROJECT_TIMESTAMPS = (
    "ready_at",
    "review_ends_at",
    "locked_at",
    "seat_open_since",
    "last_lead_action_at",
)
FORMATION_TIMESTAMPS = ("review_ends_at", "reg_cutoff_at", "late_round_at")
REMOVED_SETTINGS = (
    "lead_fit_threshold",
    "viable_ratio",
    "core_ratio",
    "registration_buffer_hours",
    "formation_min_notice_hours",
    "review_hours",
    "review_min_hours",
    "late_core_hours",
    "late_review_hours",
    "seat_fill_hours",
    "late_claim_grace_hours",
    "presence_check_hours",
    "presence_confirm_hours",
    "lead_offer_hours",
    "reserve_offer_hours",
    "confirm_reminder_hours",
    "project_lead_offer_days",
)


def upgrade() -> None:
    for table in ("lead_fits", "lead_offers", "swap_requests", "merge_proposals"):
        op.drop_table(table)
    for table, column, _, _ in ENUMS:
        op.drop_constraint(f"{column}_enum", table, type_="check")
    op.execute(
        "UPDATE projects SET team_status = 'formed' "
        "WHERE team_status IN ('forming', 'review', 'locked')"
    )
    op.execute("UPDATE event_formations SET phase = 'formed' WHERE phase IN ('review', 'locked')")
    op.execute("UPDATE projects SET origin = 'formation' WHERE origin IN ('claim', 'late_claim')")
    op.execute(
        "UPDATE project_members SET joined_via = CASE "
        "WHEN joined_via = 'claim' THEN 'formation' "
        "WHEN joined_via IN ('core_invite', 'fill_invite') THEN 'invite' "
        "ELSE 'refill' END "
        "WHERE joined_via IN ('claim', 'core_invite', 'fill_invite', "
        "'reserve_add', 'reserve_offer', 'swap', 'merge')"
    )
    op.execute(
        "UPDATE project_members SET left_reason = 'left' WHERE left_reason IN ('swapped', 'merged')"
    )
    op.execute("UPDATE invites SET kind = 'team'")
    for table, column, _, values in ENUMS:
        op.create_check_constraint(f"{column}_enum", table, f"{column} IN ({values})")
    op.execute(
        "DELETE FROM join_requests WHERE event_id IS NOT NULL OR project_id IN "
        "(SELECT id FROM projects WHERE team_status IS NOT NULL)"
    )
    op.drop_index("uq_join_requests_event_id_user_id_live", table_name="join_requests")
    op.drop_index("uq_join_requests_project_id_user_id_live", table_name="join_requests")
    op.drop_constraint("fk_join_requests_event_id_events", "join_requests", type_="foreignkey")
    op.drop_column("join_requests", "event_id")
    op.create_index(
        "uq_join_requests_project_id_user_id_live",
        "join_requests",
        ["project_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_table(
        "parties",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("closed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.add_column("enrollments", sa.Column("party_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_enrollments_party_id_parties",
        "enrollments",
        "parties",
        ["party_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column("invites", sa.Column("party_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_invites_party_id_parties",
        "invites",
        "parties",
        ["party_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.alter_column("invites", "project_id", existing_type=sa.Uuid(), nullable=True)
    op.create_check_constraint(
        "target",
        "invites",
        "(kind = 'party' AND party_id IS NOT NULL AND project_id IS NULL) OR "
        "(kind = 'team' AND project_id IS NOT NULL AND party_id IS NULL)",
    )
    op.create_index(
        "uq_invites_party_id_user_id_live",
        "invites",
        ["party_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    for column in PROJECT_TIMESTAMPS:
        op.drop_column("projects", column)
    op.drop_constraint(
        "fk_projects_formed_in_run_id_formation_runs", "projects", type_="foreignkey"
    )
    op.drop_column("projects", "formed_in_run_id")
    op.drop_column("projects", "lead_succession_excluded")
    for column in FORMATION_TIMESTAMPS:
        op.drop_column("event_formations", column)
    op.drop_constraint("rounds_nonnegative", "event_formations", type_="check")
    op.drop_column("event_formations", "rounds")
    op.drop_column("enrollments", "reserve_rank")
    op.drop_constraint("kind_enum", "formation_runs", type_="check")
    op.drop_column("formation_runs", "kind")
    op.drop_column("formation_runs", "round")
    op.drop_constraint("proposal", "project_members", type_="check")
    op.drop_constraint("confirm_kind_enum", "project_members", type_="check")
    op.drop_constraint("proposal_reason_length", "project_members", type_="check")
    op.drop_column("project_members", "confirm_kind")
    op.drop_column("project_members", "proposal_reason")
    op.alter_column("project_members", "confirm_by", new_column_name="accept_by")
    op.create_check_constraint(
        "proposal", "project_members", "state <> 'proposed' OR accept_by IS NOT NULL"
    )
    op.execute(
        sa.text("DELETE FROM settings WHERE key = ANY(:keys)").bindparams(
            keys=list(REMOVED_SETTINGS)
        )
    )


def downgrade() -> None:
    op.drop_constraint("proposal", "project_members", type_="check")
    op.alter_column("project_members", "accept_by", new_column_name="confirm_by")
    op.add_column("project_members", sa.Column("confirm_kind", sa.Text(), nullable=True))
    op.add_column("project_members", sa.Column("proposal_reason", sa.Text(), nullable=True))
    op.execute("UPDATE project_members SET confirm_kind = 'placement' WHERE state = 'proposed'")
    op.create_check_constraint(
        "confirm_kind_enum",
        "project_members",
        "confirm_kind IN ('placement', 'reconfirm', 'presence')",
    )
    op.create_check_constraint(
        "proposal_reason_length", "project_members", "char_length(proposal_reason) <= 1000"
    )
    op.create_check_constraint(
        "proposal",
        "project_members",
        "state <> 'proposed' OR (confirm_by IS NOT NULL AND confirm_kind IS NOT NULL)",
    )
    op.add_column(
        "formation_runs",
        sa.Column("round", sa.Integer(), nullable=False, server_default=sa.text("1")),
    )
    op.add_column(
        "formation_runs",
        sa.Column("kind", sa.Text(), nullable=False, server_default=sa.text("'main'")),
    )
    op.alter_column("formation_runs", "round", server_default=None)
    op.alter_column("formation_runs", "kind", server_default=None)
    op.create_check_constraint("kind_enum", "formation_runs", "kind IN ('main', 'late')")
    op.add_column("enrollments", sa.Column("reserve_rank", sa.Integer(), nullable=True))
    for column in FORMATION_TIMESTAMPS:
        op.add_column(
            "event_formations", sa.Column(column, sa.DateTime(timezone=True), nullable=True)
        )
    op.add_column(
        "event_formations",
        sa.Column("rounds", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.create_check_constraint("rounds_nonnegative", "event_formations", "rounds >= 0")
    for column in PROJECT_TIMESTAMPS:
        op.add_column("projects", sa.Column(column, sa.DateTime(timezone=True), nullable=True))
    op.add_column("projects", sa.Column("formed_in_run_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_projects_formed_in_run_id_formation_runs",
        "projects",
        "formation_runs",
        ["formed_in_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "projects",
        sa.Column(
            "lead_succession_excluded",
            sa.ARRAY(sa.Uuid()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.drop_constraint("target", "invites", type_="check")
    op.drop_index("uq_invites_party_id_user_id_live", table_name="invites")
    op.execute("DELETE FROM invites WHERE party_id IS NOT NULL")
    op.drop_constraint("fk_invites_party_id_parties", "invites", type_="foreignkey")
    op.drop_column("invites", "party_id")
    op.alter_column("invites", "project_id", existing_type=sa.Uuid(), nullable=False)
    op.drop_constraint("fk_enrollments_party_id_parties", "enrollments", type_="foreignkey")
    op.drop_column("enrollments", "party_id")
    op.drop_table("parties")
    op.drop_index("uq_join_requests_project_id_user_id_live", table_name="join_requests")
    op.add_column("join_requests", sa.Column("event_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_join_requests_event_id_events",
        "join_requests",
        "events",
        ["event_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_join_requests_event_id_user_id_live",
        "join_requests",
        ["event_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "uq_join_requests_project_id_user_id_live",
        "join_requests",
        ["project_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending' AND event_id IS NULL"),
    )
    for table, column, _, _ in ENUMS:
        op.drop_constraint(f"{column}_enum", table, type_="check")
    op.execute("UPDATE projects SET team_status = 'review' WHERE team_status = 'formed'")
    op.execute("UPDATE event_formations SET phase = 'review' WHERE phase = 'formed'")
    op.execute("UPDATE project_members SET joined_via = 'reserve_add' WHERE joined_via = 'refill'")
    op.execute(
        "UPDATE invites SET kind = CASE WHEN event_id IS NULL THEN 'project' ELSE 'fill' END"
    )
    for table, column, values, _ in ENUMS:
        op.create_check_constraint(f"{column}_enum", table, f"{column} IN ({values})")
    op.create_table(
        "lead_fits",
        sa.Column(
            "event_id", sa.Uuid(), sa.ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("fit", sa.Numeric(3, 2), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("algo_version", sa.Text(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("char_length(reason) <= 1000", name="reason_length"),
        sa.CheckConstraint("fit BETWEEN 0 AND 1", name="fit_range"),
    )
    op.create_table(
        "lead_offers",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "event_id", sa.Uuid(), sa.ForeignKey("events.id", ondelete="RESTRICT"), nullable=True
        ),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column(
            "offered_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("tier", sa.SmallInteger(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "source IN ('formation', 'succession', 'transfer')",
            name="source_enum",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'declined', 'expired', 'cancelled', 'closed')",
            name="status_enum",
        ),
        sa.CheckConstraint("char_length(reason) <= 1000", name="reason_length"),
    )
    op.create_index(
        "uq_lead_offers_project_id_live",
        "lead_offers",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_table(
        "swap_requests",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), primary_key=True),
        sa.Column(
            "event_id", sa.Uuid(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "from_team_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "from_user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "to_team_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "to_user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "requested_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("to_lead_accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("from_user_accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("to_user_accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'declined', 'expired', 'closed')",
            name="status_enum",
        ),
    )
    op.create_table(
        "merge_proposals",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), primary_key=True),
        sa.Column(
            "event_id", sa.Uuid(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("formation_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "absorbed_team_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "target_team_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("proposed_by", sa.Text(), nullable=False),
        sa.Column("absorbed_accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("target_accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("proposed_by IN ('system', 'lead')", name="proposed_by_enum"),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'declined', 'expired', 'closed')",
            name="status_enum",
        ),
    )


def _timestamps() -> list[sa.Column]:
    return [
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
    ]
