from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from procrastinate.schema import SchemaManager
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA procrastinate")
    op.execute("SET LOCAL search_path TO procrastinate, public")
    op.get_bind().exec_driver_sql(SchemaManager.get_schema().replace("%", "%%"))
    op.execute("SET LOCAL search_path TO public")
    op.create_table(
        "algo_cache",
        sa.Column("namespace", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.LargeBinary(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("namespace", "key", name=op.f("pk_algo_cache")),
    )
    op.create_table(
        "areas",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("sort", sa.Integer(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("key ~ '^[a-z][a-z0-9_]{0,31}$'", name=op.f("ck_areas_key_slug")),
        sa.CheckConstraint("char_length(name) <= 100", name=op.f("ck_areas_name_length")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_areas")),
        sa.UniqueConstraint("key", name=op.f("uq_areas_key")),
    )
    op.execute(
        """
        CREATE FUNCTION public.prevent_area_key_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.key IS DISTINCT FROM OLD.key THEN
                RAISE EXCEPTION 'Area keys are immutable' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER areas_key_immutable BEFORE UPDATE OF key ON areas "
        "FOR EACH ROW EXECUTE FUNCTION public.prevent_area_key_change()"
    )
    op.create_table(
        "parser_heartbeats",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stats", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("errors", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_parser_heartbeats")),
    )
    op.create_table(
        "recommendation_sets",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("subject_type", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("input_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("enrolled", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("others", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("algo_version", sa.Text(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'ready', 'failed')",
            name=op.f("ck_recommendation_sets_status_enum"),
        ),
        sa.CheckConstraint(
            "subject_type IN ('project', 'help_request')",
            name=op.f("ck_recommendation_sets_subject_type_enum"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recommendation_sets")),
    )
    op.create_table(
        "review_windows",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("subject_type", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("opens_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closes_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revealed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "subject_type IN ('project', 'mentorship')",
            name=op.f("ck_review_windows_subject_type_enum"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_review_windows")),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("github_id", sa.BigInteger(), nullable=False),
        sa.Column("github_login", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("avatar_url", sa.Text(), nullable=True),
        sa.Column("about_md", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "contacts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("timezone", sa.Text(), nullable=True),
        sa.Column("locale", sa.Text(), server_default=sa.text("'en'"), nullable=False),
        sa.Column("is_admin", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("banned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ban_reason", sa.Text(), nullable=True),
        sa.Column("onboarding_status", sa.Text(), nullable=False),
        sa.Column("dnd_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mentor_capacity", sa.Integer(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            (
                "jsonb_typeof(contacts) = 'object' AND "
                "jsonb_array_length(jsonb_path_query_array(contacts, '$.keyvalue()'))"
                " <= 10"
            ),
            name=op.f("ck_users_contacts"),
        ),
        sa.CheckConstraint("locale IN ('en', 'ru')", name=op.f("ck_users_locale_enum")),
        sa.CheckConstraint(
            ("onboarding_status IN ('not_started', 'in_progress', 'completed', 'reopened')"),
            name=op.f("ck_users_onboarding_status_enum"),
        ),
        sa.CheckConstraint("char_length(about_md) <= 5000", name=op.f("ck_users_about_md_length")),
        sa.CheckConstraint(
            "char_length(ban_reason) <= 1000", name=op.f("ck_users_ban_reason_length")
        ),
        sa.CheckConstraint(
            "char_length(display_name) <= 100", name=op.f("ck_users_display_name_length")
        ),
        sa.CheckConstraint("mentor_capacity >= 0", name=op.f("ck_users_mentor_capacity")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("github_id", name=op.f("uq_users_github_id")),
    )
    op.create_table(
        "activity_entries",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description_md", sa.Text(), nullable=False),
        sa.Column("links", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("area_id", sa.Uuid(), nullable=True),
        sa.Column("occurred_on", sa.Date(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "jsonb_typeof(links) = 'array' AND jsonb_array_length(links) <= 10",
            name=op.f("ck_activity_entries_links_length"),
        ),
        sa.CheckConstraint(
            "type IN ('learning', 'course', 'talk', 'writeup', 'oss', 'other')",
            name=op.f("ck_activity_entries_type_enum"),
        ),
        sa.CheckConstraint(
            "char_length(description_md) <= 5000",
            name=op.f("ck_activity_entries_description_md_length"),
        ),
        sa.CheckConstraint(
            "char_length(title) <= 200", name=op.f("ck_activity_entries_title_length")
        ),
        sa.ForeignKeyConstraint(
            ["area_id"],
            ["areas.id"],
            name=op.f("fk_activity_entries_area_id_areas"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_activity_entries_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_activity_entries")),
    )
    op.create_table(
        "admin_alerts",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("subject_type", sa.Text(), nullable=True),
        sa.Column("subject_id", sa.Uuid(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("resolved_by", sa.Uuid(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "severity IN ('info', 'warning', 'critical')",
            name=op.f("ck_admin_alerts_severity_enum"),
        ),
        sa.CheckConstraint(
            "status IN ('open', 'resolved')", name=op.f("ck_admin_alerts_status_enum")
        ),
        sa.CheckConstraint(
            (
                "type IN ('formation_undersized', 'formation_failed', "
                "'team_below_min', 'event_dates_changed', 'forced_lead', "
                "'lead_below_threshold', 'result_overdue', 'import_review', "
                "'event_cancelled_with_teams', 'team_size_conflict', "
                "'suspicious_activity', 'restriction_violation', 'review_reported', "
                "'banned_member_in_active_team', 'algo_failure', 'job_failing', "
                "'parser_down')"
            ),
            name=op.f("ck_admin_alerts_type_enum"),
        ),
        sa.CheckConstraint(
            "char_length(resolution_note) <= 1000",
            name=op.f("ck_admin_alerts_resolution_note_length"),
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by"],
            ["users.id"],
            name=op.f("fk_admin_alerts_resolved_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admin_alerts")),
    )
    op.create_index(
        "uq_admin_alerts_dedupe_key_live",
        "admin_alerts",
        ["dedupe_key"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_table(
        "area_score_events",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("area_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("old_effective", sa.Numeric(precision=4, scale=2), nullable=False),
        sa.Column("new_effective", sa.Numeric(precision=4, scale=2), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("algo_version", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('calculated', 'override_set', 'override_removed')",
            name=op.f("ck_area_score_events_kind_enum"),
        ),
        sa.CheckConstraint(
            "char_length(reason) <= 1000", name=op.f("ck_area_score_events_reason_length")
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            name=op.f("fk_area_score_events_actor_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["area_id"],
            ["areas.id"],
            name=op.f("fk_area_score_events_area_id_areas"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_area_score_events_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_area_score_events")),
    )
    op.create_table(
        "area_scores",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("area_id", sa.Uuid(), nullable=False),
        sa.Column("calculated", sa.Numeric(precision=4, scale=2), nullable=False),
        sa.Column("calc_reason", sa.Text(), nullable=False),
        sa.Column("override", sa.Numeric(precision=4, scale=2), nullable=True),
        sa.Column("override_by", sa.Uuid(), nullable=True),
        sa.Column("override_reason", sa.Text(), nullable=True),
        sa.Column("override_evidence", sa.Text(), nullable=True),
        sa.Column("override_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            ("override_evidence IN ('interview', 'github_review', 'cv_review', 'other')"),
            name=op.f("ck_area_scores_override_evidence_enum"),
        ),
        sa.CheckConstraint(
            "calculated BETWEEN 0 AND 10", name=op.f("ck_area_scores_calculated_range")
        ),
        sa.CheckConstraint(
            "char_length(override_reason) <= 1000",
            name=op.f("ck_area_scores_override_reason_length"),
        ),
        sa.CheckConstraint("override BETWEEN 0 AND 10", name=op.f("ck_area_scores_override_range")),
        sa.ForeignKeyConstraint(
            ["area_id"],
            ["areas.id"],
            name=op.f("fk_area_scores_area_id_areas"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["override_by"],
            ["users.id"],
            name=op.f("fk_area_scores_override_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_area_scores_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("user_id", "area_id", name=op.f("pk_area_scores")),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("actor_kind", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("before", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            (
                "action IN ('user_edit', 'user_ban', 'user_unban', 'admin_grant', "
                "'admin_revoke', 'area_create', 'area_edit', 'area_archive', "
                "'area_unarchive', 'quiz_create', 'quiz_edit', 'quiz_clone', "
                "'quiz_validate', 'quiz_publish', 'quiz_delete', 'onboarding_reopen',"
                " 'member_review_edit', 'member_review_complete', "
                "'score_override_set', 'score_override_remove', 'xp_adjust', "
                "'xp_adjust_reverse', 'member_recompute', 'flag_pardon', "
                "'flag_punish', 'fact_exclude', 'confirmations_revoke', "
                "'restriction_apply', 'restriction_lift', 'event_create', "
                "'event_edit', 'event_unlock_field', 'event_publish', 'event_reject',"
                " 'event_merge', 'event_cancel', 'formation_edit', 'formation_run', "
                "'reserve_reorder', 'project_create', 'project_disband', "
                "'project_status_set', 'member_add', 'member_remove', 'member_move', "
                "'lead_assign', 'lead_transfer', 'lead_step_down', 'result_submit', "
                "'result_edit', 'result_verify', 'result_reject', 'review_remove', "
                "'alert_resolve', 'setting_set', 'job_retry', 'system_transition')"
            ),
            name=op.f("ck_audit_log_action_enum"),
        ),
        sa.CheckConstraint(
            "actor_kind IN ('user', 'admin', 'system', 'service')",
            name=op.f("ck_audit_log_actor_kind_enum"),
        ),
        sa.CheckConstraint(
            (
                "entity_type IN ('user', 'area', 'quiz', 'onboarding', "
                "'member_review', 'area_score', 'xp_adjustment', 'flag', 'fact', "
                "'confirmation', 'restriction', 'event', 'formation', 'enrollment', "
                "'project', 'project_member', 'invite', 'join_request', 'lead_offer',"
                " 'swap_request', 'merge_proposal', 'project_result', 'review', "
                "'admin_alert', 'setting', 'job', 'attendance', 'help_request', "
                "'help_offer', 'mentorship')"
            ),
            name=op.f("ck_audit_log_entity_type_enum"),
        ),
        sa.CheckConstraint("char_length(reason) <= 1000", name=op.f("ck_audit_log_reason_length")),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            name=op.f("fk_audit_log_actor_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    op.create_table(
        "confirmations",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("fact_type", sa.Text(), nullable=False),
        sa.Column("fact_id", sa.Uuid(), nullable=False),
        sa.Column("subject_user_id", sa.Uuid(), nullable=False),
        sa.Column("confirmer_id", sa.Uuid(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("ignored", sa.Boolean(), nullable=False),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "fact_type IN ('attendance', 'project_member', 'mentorship_side')",
            name=op.f("ck_confirmations_fact_type_enum"),
        ),
        sa.CheckConstraint(
            (
                "source IN ('peer', 'lead', 'organizer', 'host', 'learner', 'mentor',"
                " 'system', 'admin')"
            ),
            name=op.f("ck_confirmations_source_enum"),
        ),
        sa.CheckConstraint(
            "confirmer_id IS NULL OR confirmer_id <> subject_user_id",
            name=op.f("ck_confirmations_not_self"),
        ),
        sa.ForeignKeyConstraint(
            ["confirmer_id"],
            ["users.id"],
            name=op.f("fk_confirmations_confirmer_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["subject_user_id"],
            ["users.id"],
            name=op.f("fk_confirmations_subject_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_confirmations")),
    )
    op.create_index(
        "uq_confirmations_fact_type_fact_id_confirmer_id_live",
        "confirmations",
        ["fact_type", "fact_id", "confirmer_id"],
        unique=True,
        postgresql_where=sa.text("withdrawn_at IS NULL"),
        postgresql_nulls_not_distinct=True,
    )
    op.create_table(
        "cv_files",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("mime", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("text_status", sa.Text(), nullable=False),
        sa.Column("text_error", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "text_status IN ('pending', 'ok', 'failed')", name=op.f("ck_cv_files_text_status_enum")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_cv_files_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_cv_files")),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("formation_enabled", sa.Boolean(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("description_md", sa.Text(), nullable=True),
        sa.Column("organizer", sa.Text(), nullable=True),
        sa.Column("website_url", sa.Text(), nullable=True),
        sa.Column("registration_url", sa.Text(), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("registration_opens_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("registration_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column("format", sa.Text(), nullable=True),
        sa.Column("location", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("language", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("eligibility", sa.Text(), nullable=True),
        sa.Column("themes", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("team_size_min", sa.Integer(), nullable=True),
        sa.Column("team_size_max", sa.Integer(), nullable=True),
        sa.Column("prize", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("difficulty", sa.Numeric(precision=3, scale=1), nullable=True),
        sa.Column("difficulty_rationale", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("review_reasons", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=3, scale=2), nullable=True),
        sa.Column("unmapped_areas", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "format IN ('online', 'offline', 'hybrid')", name=op.f("ck_events_format_enum")
        ),
        sa.CheckConstraint(
            (
                "review_reasons <@ ARRAY['low_confidence', 'possible_duplicate', "
                "'unmapped_areas', 'manual']::text[]"
            ),
            name=op.f("ck_events_review_reasons_enum"),
        ),
        sa.CheckConstraint(
            (
                "kind IN ('hackathon', 'contest', 'conference', 'meetup', 'workshop',"
                " 'course', 'other')"
            ),
            name=op.f("ck_events_kind_enum"),
        ),
        sa.CheckConstraint(
            "origin IN ('import', 'manual', 'member')", name=op.f("ck_events_origin_enum")
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'published', 'cancelled', 'archived')",
            name=op.f("ck_events_status_enum"),
        ),
        sa.CheckConstraint(
            (
                "(team_size_min IS NULL OR team_size_min BETWEEN 1 AND 20) AND "
                "(team_size_max IS NULL OR team_size_max BETWEEN 1 AND 20) AND "
                "(team_size_min IS NULL OR team_size_max IS NULL OR team_size_min <= "
                "team_size_max)"
            ),
            name=op.f("ck_events_team_size"),
        ),
        sa.CheckConstraint(
            "char_length(description_md) <= 20000", name=op.f("ck_events_description_md_length")
        ),
        sa.CheckConstraint("char_length(summary) <= 500", name=op.f("ck_events_summary_length")),
        sa.CheckConstraint("char_length(title) <= 200", name=op.f("ck_events_title_length")),
        sa.CheckConstraint("confidence BETWEEN 0 AND 1", name=op.f("ck_events_confidence_range")),
        sa.CheckConstraint("difficulty BETWEEN 1 AND 10", name=op.f("ck_events_difficulty_range")),
        sa.CheckConstraint("ends_at >= starts_at", name=op.f("ck_events_dates")),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_events_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
        sa.UniqueConstraint("slug", name=op.f("uq_events_slug")),
    )
    op.create_table(
        "fact_awards",
        sa.Column("fact_type", sa.Text(), nullable=False),
        sa.Column("fact_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("xp", sa.Numeric(), nullable=False),
        sa.Column("activity", sa.Numeric(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("algo_version", sa.Text(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            ("fact_type IN ('attendance', 'project_member', 'mentorship_side', 'activity_entry')"),
            name=op.f("ck_fact_awards_fact_type_enum"),
        ),
        sa.CheckConstraint(
            "char_length(reason) <= 1000", name=op.f("ck_fact_awards_reason_length")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_fact_awards_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("fact_type", "fact_id", "user_id", name=op.f("pk_fact_awards")),
    )
    op.create_table(
        "feed_items",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("subject_type", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("area_ids", sa.ARRAY(sa.Uuid()), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("hidden_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            (
                "kind IN ('project_recruiting', 'project_shipped', 'help_request', "
                "'event_going', 'event_new', 'mentorship_completed')"
            ),
            name=op.f("ck_feed_items_kind_enum"),
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            name=op.f("fk_feed_items_actor_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feed_items")),
    )
    op.create_table(
        "flags",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("subject_type", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("related_user_ids", sa.ARRAY(sa.Uuid()), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("algo_version", sa.Text(), nullable=False),
        sa.Column("resolved_by", sa.Uuid(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("penalty", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "severity IN ('low', 'medium', 'high')", name=op.f("ck_flags_severity_enum")
        ),
        sa.CheckConstraint(
            "status IN ('to_check', 'pardoned', 'punished')", name=op.f("ck_flags_status_enum")
        ),
        sa.CheckConstraint(
            (
                "subject_type IN ('attendance', 'project_member', 'mentorship', "
                "'confirmation', 'activity_entry', 'review', 'profile', 'ring')"
            ),
            name=op.f("ck_flags_subject_type_enum"),
        ),
        sa.CheckConstraint("char_length(reason) <= 1000", name=op.f("ck_flags_reason_length")),
        sa.CheckConstraint(
            "char_length(resolution_note) <= 1000", name=op.f("ck_flags_resolution_note_length")
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by"],
            ["users.id"],
            name=op.f("fk_flags_resolved_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_flags_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_flags")),
    )
    op.create_index(
        "uq_flags_subject_type_subject_id_live",
        "flags",
        ["subject_type", "subject_id"],
        unique=True,
        postgresql_where=sa.text("status = 'to_check'"),
    )
    op.create_table(
        "github_snapshots",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("profile", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("repos", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("languages", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("stats", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("manual_refresh_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('ok', 'error')", name=op.f("ck_github_snapshots_status_enum")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_github_snapshots_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_github_snapshots")),
    )
    op.create_table(
        "help_requests",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("learner_id", sa.Uuid(), nullable=False),
        sa.Column("area_id", sa.Uuid(), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("format", sa.Text(), nullable=False),
        sa.Column("target_mentor_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "format IN ('online', 'offline', 'any')", name=op.f("ck_help_requests_format_enum")
        ),
        sa.CheckConstraint(
            "status IN ('open', 'direct', 'matched', 'cancelled', 'expired')",
            name=op.f("ck_help_requests_status_enum"),
        ),
        sa.CheckConstraint("char_length(goal) <= 1000", name=op.f("ck_help_requests_goal_length")),
        sa.CheckConstraint("char_length(topic) <= 200", name=op.f("ck_help_requests_topic_length")),
        sa.ForeignKeyConstraint(
            ["area_id"],
            ["areas.id"],
            name=op.f("fk_help_requests_area_id_areas"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["learner_id"],
            ["users.id"],
            name=op.f("fk_help_requests_learner_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_mentor_id"],
            ["users.id"],
            name=op.f("fk_help_requests_target_mentor_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_help_requests")),
    )
    op.create_index(
        "uq_help_requests_learner_id_area_id_live",
        "help_requests",
        ["learner_id", "area_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('open', 'direct')"),
    )
    op.create_table(
        "member_metrics",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("xp", sa.Numeric(), nullable=False),
        sa.Column("activity", sa.Numeric(), nullable=False),
        sa.Column("algo_version", sa.Text(), nullable=False),
        sa.Column("input_hash", sa.Text(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_member_metrics_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_member_metrics")),
    )
    op.create_table(
        "member_reviews",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_by", sa.Uuid(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("admin_note", sa.Text(), nullable=True),
        sa.Column("discrepancies", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'reviewed')", name=op.f("ck_member_reviews_status_enum")
        ),
        sa.CheckConstraint(
            "char_length(admin_note) <= 1000", name=op.f("ck_member_reviews_admin_note_length")
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by"],
            ["users.id"],
            name=op.f("fk_member_reviews_reviewed_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_member_reviews_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_member_reviews")),
    )
    op.create_table(
        "mentor_offers",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("area_id", sa.Uuid(), nullable=False),
        sa.Column("topics", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["area_id"],
            ["areas.id"],
            name=op.f("fk_mentor_offers_area_id_areas"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_mentor_offers_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "area_id", name=op.f("pk_mentor_offers")),
    )
    op.create_table(
        "notification_settings",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("web", sa.Boolean(), nullable=False),
        sa.Column("telegram", sa.Boolean(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            (
                "type IN ('invite_received', 'invite_accepted', 'invite_declined', "
                "'invite_expired', 'invite_closed', 'join_request_received', "
                "'join_request_accepted', 'join_request_rejected', 'formation_soon', "
                "'formation_rescheduled', 'team_proposed', 'team_reconfirm', "
                "'presence_check', 'lead_offer', 'lead_offer_declined', "
                "'formation_ready', 'reserve_placed', 'confirm_reminder', "
                "'review_ending', 'placement_lapsed', 'swap_request_received', "
                "'merge_proposed', 'swap_result', 'merge_result', "
                "'placement_changed', 'team_locked', 'team_member_joined', "
                "'team_member_left', 'seat_opened', 'seat_autofilled', "
                "'reserve_offer', 'lead_opportunity', 'lead_transferred', "
                "'lead_stepped_down', 'lead_assigned', 'team_disbanded', "
                "'team_status_changed', 'event_changed', 'event_cancelled', "
                "'attendance_confirm_digest', 'project_invite_received', "
                "'project_join_request_received', 'project_member_joined', "
                "'project_member_left', 'project_member_removed', 'project_idle', "
                "'project_paused', 'project_released', "
                "'contributions_confirm_digest', 'project_lead_offer', "
                "'project_lead_claimable', 'help_request_match', "
                "'help_request_direct', 'help_request_declined', "
                "'help_offer_received', 'help_offer_accepted', 'help_offer_closed', "
                "'help_request_expired', 'mentorship_started', 'mentorship_idle', "
                "'mentorship_stalled', 'mentorship_close_proposed', "
                "'mentorship_closed', 'penalty_applied', 'restriction_lifted', "
                "'result_due', 'reviews_open', 'reviews_pending', 'review_received', "
                "'review_removed', 'score_changed', 'scores_reviewed', "
                "'onboarding_reopened', 'telegram_link_broken', 'admin_alert', "
                "'admin_digest')"
            ),
            name=op.f("ck_notification_settings_type_enum"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_notification_settings_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "type", name=op.f("pk_notification_settings")),
    )
    op.create_table(
        "notifications",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            (
                "type IN ('invite_received', 'invite_accepted', 'invite_declined', "
                "'invite_expired', 'invite_closed', 'join_request_received', "
                "'join_request_accepted', 'join_request_rejected', 'formation_soon', "
                "'formation_rescheduled', 'team_proposed', 'team_reconfirm', "
                "'presence_check', 'lead_offer', 'lead_offer_declined', "
                "'formation_ready', 'reserve_placed', 'confirm_reminder', "
                "'review_ending', 'placement_lapsed', 'swap_request_received', "
                "'merge_proposed', 'swap_result', 'merge_result', "
                "'placement_changed', 'team_locked', 'team_member_joined', "
                "'team_member_left', 'seat_opened', 'seat_autofilled', "
                "'reserve_offer', 'lead_opportunity', 'lead_transferred', "
                "'lead_stepped_down', 'lead_assigned', 'team_disbanded', "
                "'team_status_changed', 'event_changed', 'event_cancelled', "
                "'attendance_confirm_digest', 'project_invite_received', "
                "'project_join_request_received', 'project_member_joined', "
                "'project_member_left', 'project_member_removed', 'project_idle', "
                "'project_paused', 'project_released', "
                "'contributions_confirm_digest', 'project_lead_offer', "
                "'project_lead_claimable', 'help_request_match', "
                "'help_request_direct', 'help_request_declined', "
                "'help_offer_received', 'help_offer_accepted', 'help_offer_closed', "
                "'help_request_expired', 'mentorship_started', 'mentorship_idle', "
                "'mentorship_stalled', 'mentorship_close_proposed', "
                "'mentorship_closed', 'penalty_applied', 'restriction_lifted', "
                "'result_due', 'reviews_open', 'reviews_pending', 'review_received', "
                "'review_removed', 'score_changed', 'scores_reviewed', "
                "'onboarding_reopened', 'telegram_link_broken', 'admin_alert', "
                "'admin_digest')"
            ),
            name=op.f("ck_notifications_type_enum"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_notifications_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
        sa.UniqueConstraint("dedupe_key", name=op.f("uq_notifications_dedupe_key")),
    )
    op.create_table(
        "quizzes",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'published', 'archived')", name=op.f("ck_quizzes_status_enum")
        ),
        sa.CheckConstraint("char_length(title) <= 200", name=op.f("ck_quizzes_title_length")),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_quizzes_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_quizzes")),
        sa.UniqueConstraint("version", name=op.f("uq_quizzes_version")),
    )
    op.create_index(
        "uq_quizzes_status_live",
        "quizzes",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'published'"),
    )
    op.create_table(
        "reviews",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("window_id", sa.Uuid(), nullable=True),
        sa.Column("subject_type", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("reviewer_id", sa.Uuid(), nullable=False),
        sa.Column("reviewee_id", sa.Uuid(), nullable=False),
        sa.Column("context", sa.Text(), nullable=False),
        sa.Column("rating", sa.SmallInteger(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.Column("revealed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("removed_by", sa.Uuid(), nullable=True),
        sa.Column("removed_reason", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "context IN ('member', 'lead', 'mentor', 'host', 'speaker')",
            name=op.f("ck_reviews_context_enum"),
        ),
        sa.CheckConstraint(
            "subject_type IN ('project', 'mentorship', 'event')",
            name=op.f("ck_reviews_subject_type_enum"),
        ),
        sa.CheckConstraint("char_length(comment) <= 2000", name=op.f("ck_reviews_comment_length")),
        sa.CheckConstraint(
            "char_length(removed_reason) <= 1000", name=op.f("ck_reviews_removed_reason_length")
        ),
        sa.CheckConstraint("rating BETWEEN 1 AND 10", name=op.f("ck_reviews_rating_range")),
        sa.CheckConstraint("reviewer_id <> reviewee_id", name=op.f("ck_reviews_not_self")),
        sa.ForeignKeyConstraint(
            ["removed_by"],
            ["users.id"],
            name=op.f("fk_reviews_removed_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["reviewee_id"],
            ["users.id"],
            name=op.f("fk_reviews_reviewee_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_id"],
            ["users.id"],
            name=op.f("fk_reviews_reviewer_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["window_id"],
            ["review_windows.id"],
            name=op.f("fk_reviews_window_id_review_windows"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reviews")),
        sa.UniqueConstraint(
            "subject_type",
            "subject_id",
            "window_id",
            "reviewer_id",
            "reviewee_id",
            name=op.f("uq_reviews_subject_type_subject_id_window_id_reviewer_id_reviewee_id"),
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_table(
        "sessions",
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_sessions_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("token_hash", name=op.f("pk_sessions")),
    )
    op.create_table(
        "settings",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            (
                "key IN ('activity_window_days', 'lead_fit_threshold', "
                "'viable_ratio', 'core_ratio', 'team_size_default_min', "
                "'team_size_default_max', 'mentor_min_score', 'mentor_min_gap', "
                "'mentor_capacity_default', 'mentor_notify_top_n', "
                "'registration_buffer_hours', 'formation_lead_hours', "
                "'formation_min_notice_hours', 'review_hours', 'review_min_hours', "
                "'late_core_hours', 'late_review_hours', 'seat_fill_hours', "
                "'late_claim_grace_hours', 'presence_check_hours', "
                "'presence_confirm_hours', 'lead_offer_hours', 'reserve_offer_hours',"
                " 'confirm_reminder_hours', 'invite_ttl_days', 'results_due_days', "
                "'results_close_days', 'review_window_days', "
                "'score_change_notify_delta', 'member_review_reminder_days', "
                "'import_auto_publish_confidence', 'parser_heartbeat_max_age_hours', "
                "'session_ttl_days', 'github_manual_refresh_min_interval_minutes', "
                "'cv_max_bytes', 'event_archive_after_days', 'attendance_claim_days',"
                " 'project_size_max', 'projects_led_max', 'project_idle_days', "
                "'project_review_min_days', 'project_lead_offer_days', "
                "'mentee_active_max', 'help_request_ttl_days', "
                "'mentorship_idle_days', 'confirmation_withdraw_days')"
            ),
            name=op.f("ck_settings_key_enum"),
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["users.id"],
            name=op.f("fk_settings_updated_by_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_settings")),
    )
    op.create_table(
        "telegram_link_codes",
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_telegram_link_codes_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_telegram_link_codes")),
    )
    op.create_table(
        "telegram_links",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.Text(), nullable=True),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("broken_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_telegram_links_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_telegram_links")),
        sa.UniqueConstraint("telegram_user_id", name=op.f("uq_telegram_links_telegram_user_id")),
    )
    op.create_table(
        "enrollments",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("preferred_area_id", sa.Uuid(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("willing_to_lead", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("reserve_rank", sa.Integer(), nullable=True),
        sa.Column("status_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("source IN ('self', 'invite')", name=op.f("ck_enrollments_source_enum")),
        sa.CheckConstraint(
            ("status IN ('active', 'placed', 'reserve', 'withdrawn', 'lapsed', 'expired')"),
            name=op.f("ck_enrollments_status_enum"),
        ),
        sa.CheckConstraint("char_length(note) <= 1000", name=op.f("ck_enrollments_note_length")),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name=op.f("fk_enrollments_event_id_events"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["preferred_area_id"],
            ["areas.id"],
            name=op.f("fk_enrollments_preferred_area_id_areas"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_enrollments_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_enrollments")),
    )
    op.create_index(
        "uq_enrollments_event_id_user_id_live",
        "enrollments",
        ["event_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('active', 'placed', 'reserve')"),
    )
    op.create_table(
        "event_areas",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("area_id", sa.Uuid(), nullable=False),
        sa.Column("importance", sa.Text(), nullable=False),
        sa.Column("headcount", sa.Integer(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "importance IN ('primary', 'required', 'nice')",
            name=op.f("ck_event_areas_importance_enum"),
        ),
        sa.ForeignKeyConstraint(
            ["area_id"],
            ["areas.id"],
            name=op.f("fk_event_areas_area_id_areas"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name=op.f("fk_event_areas_event_id_events"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("event_id", "area_id", name=op.f("pk_event_areas")),
    )
    op.create_table(
        "event_attendances",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("proof_links", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("trust", sa.Text(), nullable=False),
        sa.Column("status_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            ("jsonb_typeof(proof_links) = 'array' AND jsonb_array_length(proof_links) <= 10"),
            name=op.f("ck_event_attendances_proof_links_length"),
        ),
        sa.CheckConstraint(
            "role IN ('attendee', 'speaker', 'organizer', 'host', 'volunteer')",
            name=op.f("ck_event_attendances_role_enum"),
        ),
        sa.CheckConstraint(
            "status IN ('going', 'attended', 'cancelled')",
            name=op.f("ck_event_attendances_status_enum"),
        ),
        sa.CheckConstraint(
            "trust IN ('self', 'peer', 'trusted')", name=op.f("ck_event_attendances_trust_enum")
        ),
        sa.CheckConstraint(
            "char_length(note) <= 1000", name=op.f("ck_event_attendances_note_length")
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name=op.f("fk_event_attendances_event_id_events"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_event_attendances_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_attendances")),
        sa.UniqueConstraint(
            "event_id", "user_id", name=op.f("uq_event_attendances_event_id_user_id")
        ),
    )
    op.create_table(
        "event_field_locks",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("field", sa.Text(), nullable=False),
        sa.Column("locked_by", sa.Uuid(), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name=op.f("fk_event_field_locks_event_id_events"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["locked_by"],
            ["users.id"],
            name=op.f("fk_event_field_locks_locked_by_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("event_id", "field", name=op.f("pk_event_field_locks")),
    )
    op.create_table(
        "event_revisions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("diff", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("ignored_fields", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "origin IN ('import', 'admin', 'member', 'system')",
            name=op.f("ck_event_revisions_origin_enum"),
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            name=op.f("fk_event_revisions_actor_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name=op.f("fk_event_revisions_event_id_events"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_revisions")),
    )
    op.create_table(
        "event_source_people",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("github_login", sa.Text(), nullable=True),
        sa.Column("telegram_username", sa.Text(), nullable=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("place", sa.Integer(), nullable=True),
        sa.Column("matched_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('attendee', 'speaker', 'organizer', 'winner')",
            name=op.f("ck_event_source_people_role_enum"),
        ),
        sa.CheckConstraint(
            "char_length(display_name) <= 100",
            name=op.f("ck_event_source_people_display_name_length"),
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name=op.f("fk_event_source_people_event_id_events"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["matched_user_id"],
            ["users.id"],
            name=op.f("fk_event_source_people_matched_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_source_people")),
    )
    op.create_index(
        "uq_event_source_people_identity",
        "event_source_people",
        ["source_id", sa.literal_column("coalesce(github_login, telegram_username, display_name)")],
        unique=True,
    )
    op.create_table(
        "event_sources",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("urls", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload_hash", sa.Text(), nullable=False),
        sa.Column("extractor_version", sa.Text(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name=op.f("fk_event_sources_event_id_events"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_sources")),
        sa.UniqueConstraint("source_id", name=op.f("uq_event_sources_source_id")),
    )
    op.create_table(
        "formation_runs",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("planner", sa.Text(), nullable=False),
        sa.Column("input", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("output", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("validation_errors", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("algo_version", sa.Text(), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('main', 'late')", name=op.f("ck_formation_runs_kind_enum")),
        sa.CheckConstraint(
            "planner IN ('algo', 'fallback')", name=op.f("ck_formation_runs_planner_enum")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'applied', 'failed')",
            name=op.f("ck_formation_runs_status_enum"),
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name=op.f("fk_formation_runs_event_id_events"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_formation_runs")),
    )
    op.create_table(
        "help_offers",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("mentor_id", sa.Uuid(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'declined', 'withdrawn', 'closed')",
            name=op.f("ck_help_offers_status_enum"),
        ),
        sa.CheckConstraint(
            "char_length(message) <= 1000", name=op.f("ck_help_offers_message_length")
        ),
        sa.ForeignKeyConstraint(
            ["mentor_id"],
            ["users.id"],
            name=op.f("fk_help_offers_mentor_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["help_requests.id"],
            name=op.f("fk_help_offers_request_id_help_requests"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_help_offers")),
        sa.UniqueConstraint(
            "request_id", "mentor_id", name=op.f("uq_help_offers_request_id_mentor_id")
        ),
    )
    op.create_table(
        "lead_fits",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("fit", sa.Numeric(precision=3, scale=2), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("algo_version", sa.Text(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("char_length(reason) <= 1000", name=op.f("ck_lead_fits_reason_length")),
        sa.CheckConstraint("fit BETWEEN 0 AND 1", name=op.f("ck_lead_fits_fit_range")),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name=op.f("fk_lead_fits_event_id_events"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_lead_fits_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("event_id", "user_id", name=op.f("pk_lead_fits")),
    )
    op.create_table(
        "member_restrictions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("flag_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("lifted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            ("kind IN ('confirming', 'mentoring', 'creating_events', 'creating_projects')"),
            name=op.f("ck_member_restrictions_kind_enum"),
        ),
        sa.CheckConstraint(
            "char_length(reason) <= 1000", name=op.f("ck_member_restrictions_reason_length")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_member_restrictions_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["flag_id"],
            ["flags.id"],
            name=op.f("fk_member_restrictions_flag_id_flags"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_member_restrictions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_member_restrictions")),
    )
    op.create_table(
        "mentorships",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("mentor_id", sa.Uuid(), nullable=False),
        sa.Column("learner_id", sa.Uuid(), nullable=False),
        sa.Column("area_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("mentor_score_at_start", sa.Numeric(precision=4, scale=2), nullable=False),
        sa.Column("learner_score_at_start", sa.Numeric(precision=4, scale=2), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_checkin_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_proposed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("mentor_acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("endorsement", sa.Text(), nullable=True),
        sa.Column("mentor_trust", sa.Text(), nullable=False),
        sa.Column("learner_trust", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('mentoring', 'study_pair')", name=op.f("ck_mentorships_kind_enum")
        ),
        sa.CheckConstraint(
            "learner_trust IN ('self', 'peer', 'trusted')",
            name=op.f("ck_mentorships_learner_trust_enum"),
        ),
        sa.CheckConstraint(
            "mentor_trust IN ('self', 'peer', 'trusted')",
            name=op.f("ck_mentorships_mentor_trust_enum"),
        ),
        sa.CheckConstraint(
            "outcome IN ('reached', 'partial', 'not_reached')",
            name=op.f("ck_mentorships_outcome_enum"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'stalled', 'closing', 'closed', 'cancelled')",
            name=op.f("ck_mentorships_status_enum"),
        ),
        sa.CheckConstraint(
            "char_length(summary) <= 1000", name=op.f("ck_mentorships_summary_length")
        ),
        sa.CheckConstraint("mentor_id <> learner_id", name=op.f("ck_mentorships_not_self")),
        sa.ForeignKeyConstraint(
            ["area_id"],
            ["areas.id"],
            name=op.f("fk_mentorships_area_id_areas"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["learner_id"],
            ["users.id"],
            name=op.f("fk_mentorships_learner_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["mentor_id"],
            ["users.id"],
            name=op.f("fk_mentorships_mentor_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["help_requests.id"],
            name=op.f("fk_mentorships_request_id_help_requests"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mentorships")),
        sa.UniqueConstraint("request_id", name=op.f("uq_mentorships_request_id")),
    )
    op.create_table(
        "notification_deliveries",
        sa.Column("notification_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "channel IN ('telegram')", name=op.f("ck_notification_deliveries_channel_enum")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'failed', 'skipped')",
            name=op.f("ck_notification_deliveries_status_enum"),
        ),
        sa.ForeignKeyConstraint(
            ["notification_id"],
            ["notifications.id"],
            name=op.f("fk_notification_deliveries_notification_id_notifications"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "notification_id", "channel", name=op.f("pk_notification_deliveries")
        ),
    )
    op.create_table(
        "onboarding_submissions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("quiz_id", sa.Uuid(), nullable=False),
        sa.Column("answers", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'submitted')", name=op.f("ck_onboarding_submissions_status_enum")
        ),
        sa.ForeignKeyConstraint(
            ["quiz_id"],
            ["quizzes.id"],
            name=op.f("fk_onboarding_submissions_quiz_id_quizzes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_onboarding_submissions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_onboarding_submissions")),
    )
    op.create_index(
        "uq_onboarding_submissions_user_id_live",
        "onboarding_submissions",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'draft'"),
    )
    op.create_table(
        "questions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("quiz_id", sa.Uuid(), nullable=False),
        sa.Column("section", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("help", sa.Text(), nullable=True),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("visible_if", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            ("type IN ('single_choice', 'multi_choice', 'scale', 'text', 'number', 'url')"),
            name=op.f("ck_questions_type_enum"),
        ),
        sa.ForeignKeyConstraint(
            ["quiz_id"],
            ["quizzes.id"],
            name=op.f("fk_questions_quiz_id_quizzes"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_questions")),
        sa.UniqueConstraint("quiz_id", "key", name=op.f("uq_questions_quiz_id_key")),
    )
    op.create_table(
        "review_reports",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("review_id", sa.Uuid(), nullable=False),
        sa.Column("reporter_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('open', 'resolved')", name=op.f("ck_review_reports_status_enum")
        ),
        sa.CheckConstraint(
            "char_length(reason) <= 1000", name=op.f("ck_review_reports_reason_length")
        ),
        sa.ForeignKeyConstraint(
            ["reporter_id"],
            ["users.id"],
            name=op.f("fk_review_reports_reporter_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["review_id"],
            ["reviews.id"],
            name=op.f("fk_review_reports_review_id_reviews"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_review_reports")),
    )
    op.create_table(
        "xp_adjustments",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("amount", sa.Numeric(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("admin_id", sa.Uuid(), nullable=False),
        sa.Column("flag_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(reason) <= 1000", name=op.f("ck_xp_adjustments_reason_length")
        ),
        sa.ForeignKeyConstraint(
            ["admin_id"],
            ["users.id"],
            name=op.f("fk_xp_adjustments_admin_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["flag_id"],
            ["flags.id"],
            name=op.f("fk_xp_adjustments_flag_id_flags"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_xp_adjustments_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_xp_adjustments")),
    )
    op.create_table(
        "event_formations",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("phase", sa.Text(), nullable=False),
        sa.Column("target_override", sa.Integer(), nullable=True),
        sa.Column("formation_at_override", sa.DateTime(timezone=True), nullable=True),
        sa.Column("formation_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("review_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rounds", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_run_id", sa.Uuid(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "phase IN ('open', 'forming', 'review', 'locked', 'closed')",
            name=op.f("ck_event_formations_phase_enum"),
        ),
        sa.CheckConstraint("rounds >= 0", name=op.f("ck_event_formations_rounds_nonnegative")),
        sa.CheckConstraint(
            "target_override BETWEEN 1 AND 20", name=op.f("ck_event_formations_target_range")
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name=op.f("fk_event_formations_event_id_events"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["last_run_id"],
            ["formation_runs.id"],
            name=op.f("fk_event_formations_last_run_id_formation_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_event_formations")),
    )
    op.create_table(
        "mentorship_checkins",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("mentorship_id", sa.Uuid(), nullable=False),
        sa.Column("author_id", sa.Uuid(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("occurred_on", sa.Date(), nullable=False),
        sa.Column("minutes", sa.Integer(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(note) <= 1000", name=op.f("ck_mentorship_checkins_note_length")
        ),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["users.id"],
            name=op.f("fk_mentorship_checkins_author_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["mentorship_id"],
            ["mentorships.id"],
            name=op.f("fk_mentorship_checkins_mentorship_id_mentorships"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mentorship_checkins")),
        sa.UniqueConstraint(
            "mentorship_id",
            "author_id",
            "occurred_on",
            name=op.f("uq_mentorship_checkins_mentorship_id_author_id_occurred_on"),
        ),
    )
    op.create_table(
        "projects",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description_md", sa.Text(), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("links", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("visibility", sa.Text(), nullable=False),
        sa.Column("recruiting", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("lead_user_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("team_status", sa.Text(), nullable=True),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("formed_in_run_id", sa.Uuid(), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("seat_open_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("seats_closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_lead_action_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_progress_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "jsonb_typeof(links) = 'array' AND jsonb_array_length(links) <= 10",
            name=op.f("ck_projects_links_length"),
        ),
        sa.CheckConstraint(
            "origin IN ('standalone', 'claim', 'formation', 'late_claim', 'admin')",
            name=op.f("ck_projects_origin_enum"),
        ),
        sa.CheckConstraint(
            (
                "status IN ('idea', 'active', 'shipped', 'paused', 'abandoned', "
                "'disbanded', 'cancelled')"
            ),
            name=op.f("ck_projects_status_enum"),
        ),
        sa.CheckConstraint(
            (
                "team_status IN ('forming', 'review', 'locked', 'active', 'finished',"
                " 'withdrawn', 'disbanded', 'cancelled')"
            ),
            name=op.f("ck_projects_team_status_enum"),
        ),
        sa.CheckConstraint(
            "visibility IN ('public', 'members')", name=op.f("ck_projects_visibility_enum")
        ),
        sa.CheckConstraint(
            "char_length(description_md) <= 5000", name=op.f("ck_projects_description_md_length")
        ),
        sa.CheckConstraint("char_length(goal) <= 1000", name=op.f("ck_projects_goal_length")),
        sa.CheckConstraint("char_length(name) <= 100", name=op.f("ck_projects_name_length")),
        sa.CheckConstraint(
            "team_status IS NULL OR event_id IS NOT NULL", name=op.f("ck_projects_team_event")
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name=op.f("fk_projects_event_id_events"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["formed_in_run_id"],
            ["formation_runs.id"],
            name=op.f("fk_projects_formed_in_run_id_formation_runs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["lead_user_id"],
            ["users.id"],
            name=op.f("fk_projects_lead_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_projects")),
    )
    op.create_table(
        "question_options",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("question_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("area_hints", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["question_id"],
            ["questions.id"],
            name=op.f("fk_question_options_question_id_questions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_question_options")),
        sa.UniqueConstraint("question_id", "key", name=op.f("uq_question_options_question_id_key")),
        sa.CheckConstraint(
            "jsonb_typeof(area_hints) = 'object' AND NOT jsonb_path_exists("
            "area_hints, '$.* ? (@.type() != \"number\" || @ < 0 || @ > 1)')",
            name=op.f("ck_question_options_area_hints_weights"),
        ),
    )
    op.create_table(
        "invites",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("invited_by", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("area_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("closed_reason", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('core', 'fill', 'project')", name=op.f("ck_invites_kind_enum")
        ),
        sa.CheckConstraint(
            ("status IN ('pending', 'accepted', 'declined', 'withdrawn', 'expired', 'closed')"),
            name=op.f("ck_invites_status_enum"),
        ),
        sa.CheckConstraint("char_length(message) <= 1000", name=op.f("ck_invites_message_length")),
        sa.ForeignKeyConstraint(
            ["area_id"], ["areas.id"], name=op.f("fk_invites_area_id_areas"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name=op.f("fk_invites_event_id_events"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["invited_by"],
            ["users.id"],
            name=op.f("fk_invites_invited_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_invites_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_invites_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invites")),
    )
    op.create_index(
        "uq_invites_project_id_user_id_live",
        "invites",
        ["project_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_table(
        "join_requests",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected', 'withdrawn', 'closed')",
            name=op.f("ck_join_requests_status_enum"),
        ),
        sa.CheckConstraint("char_length(note) <= 1000", name=op.f("ck_join_requests_note_length")),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name=op.f("fk_join_requests_event_id_events"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_join_requests_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_join_requests_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_join_requests")),
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
    op.create_table(
        "lead_offers",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("offered_by", sa.Uuid(), nullable=True),
        sa.Column("tier", sa.SmallInteger(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source IN ('formation', 'succession', 'transfer')",
            name=op.f("ck_lead_offers_source_enum"),
        ),
        sa.CheckConstraint(
            ("status IN ('pending', 'accepted', 'declined', 'expired', 'cancelled', 'closed')"),
            name=op.f("ck_lead_offers_status_enum"),
        ),
        sa.CheckConstraint(
            "char_length(reason) <= 1000", name=op.f("ck_lead_offers_reason_length")
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name=op.f("fk_lead_offers_event_id_events"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["offered_by"],
            ["users.id"],
            name=op.f("fk_lead_offers_offered_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_lead_offers_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_lead_offers_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_lead_offers")),
    )
    op.create_index(
        "uq_lead_offers_project_id_live",
        "lead_offers",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_table(
        "merge_proposals",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("absorbed_team_id", sa.Uuid(), nullable=False),
        sa.Column("target_team_id", sa.Uuid(), nullable=False),
        sa.Column("proposed_by", sa.Text(), nullable=False),
        sa.Column("absorbed_accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("target_accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "proposed_by IN ('system', 'lead')", name=op.f("ck_merge_proposals_proposed_by_enum")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'declined', 'expired', 'closed')",
            name=op.f("ck_merge_proposals_status_enum"),
        ),
        sa.ForeignKeyConstraint(
            ["absorbed_team_id"],
            ["projects.id"],
            name=op.f("fk_merge_proposals_absorbed_team_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name=op.f("fk_merge_proposals_event_id_events"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["formation_runs.id"],
            name=op.f("fk_merge_proposals_run_id_formation_runs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["target_team_id"],
            ["projects.id"],
            name=op.f("fk_merge_proposals_target_team_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_merge_proposals")),
    )
    op.create_table(
        "project_areas",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("area_id", sa.Uuid(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["area_id"],
            ["areas.id"],
            name=op.f("fk_project_areas_area_id_areas"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_project_areas_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("project_id", "area_id", name=op.f("pk_project_areas")),
    )
    op.create_table(
        "project_members",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("area_id", sa.Uuid(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("trust", sa.Text(), nullable=False),
        sa.Column("joined_via", sa.Text(), nullable=False),
        sa.Column("proposal_reason", sa.Text(), nullable=True),
        sa.Column("confirm_by", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirm_kind", sa.Text(), nullable=True),
        sa.Column("is_team_row", sa.Boolean(), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("left_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("left_reason", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "confirm_kind IN ('placement', 'reconfirm', 'presence')",
            name=op.f("ck_project_members_confirm_kind_enum"),
        ),
        sa.CheckConstraint(
            (
                "joined_via IN ('create', 'invite', 'claim', 'core_invite', "
                "'join_request', 'formation', 'fill_invite', 'reserve_add', "
                "'reserve_offer', 'swap', 'merge', 'admin')"
            ),
            name=op.f("ck_project_members_joined_via_enum"),
        ),
        sa.CheckConstraint(
            (
                "left_reason IN ('declined', 'lapsed', 'removed', 'left', 'swapped', "
                "'merged', 'disbanded', 'banned', 'admin')"
            ),
            name=op.f("ck_project_members_left_reason_enum"),
        ),
        sa.CheckConstraint("role IN ('lead', 'member')", name=op.f("ck_project_members_role_enum")),
        sa.CheckConstraint(
            ("state <> 'proposed' OR (confirm_by IS NOT NULL AND confirm_kind IS NOT NULL)"),
            name=op.f("ck_project_members_proposal"),
        ),
        sa.CheckConstraint(
            "state IN ('proposed', 'confirmed')", name=op.f("ck_project_members_state_enum")
        ),
        sa.CheckConstraint(
            "trust IN ('self', 'peer', 'trusted')", name=op.f("ck_project_members_trust_enum")
        ),
        sa.CheckConstraint(
            "NOT is_team_row OR event_id IS NOT NULL", name=op.f("ck_project_members_team_event")
        ),
        sa.CheckConstraint(
            "char_length(proposal_reason) <= 1000",
            name=op.f("ck_project_members_proposal_reason_length"),
        ),
        sa.ForeignKeyConstraint(
            ["area_id"],
            ["areas.id"],
            name=op.f("fk_project_members_area_id_areas"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name=op.f("fk_project_members_event_id_events"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_project_members_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_project_members_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_project_members")),
    )
    op.create_index(
        "uq_project_members_event_id_user_id_live",
        "project_members",
        ["event_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("left_at IS NULL AND is_team_row"),
    )
    op.create_index(
        "uq_project_members_project_id_live",
        "project_members",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("role = 'lead' AND left_at IS NULL"),
    )
    op.create_table(
        "project_milestones",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("due_on", sa.Date(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("done_by", sa.Uuid(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(title) <= 200", name=op.f("ck_project_milestones_title_length")
        ),
        sa.ForeignKeyConstraint(
            ["done_by"],
            ["users.id"],
            name=op.f("fk_project_milestones_done_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_project_milestones_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_project_milestones")),
    )
    op.create_table(
        "project_releases",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("notes_md", sa.Text(), nullable=False),
        sa.Column("repo_url", sa.Text(), nullable=True),
        sa.Column("demo_url", sa.Text(), nullable=True),
        sa.Column("writeup_url", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(notes_md) <= 5000", name=op.f("ck_project_releases_notes_md_length")
        ),
        sa.CheckConstraint(
            "char_length(title) <= 200", name=op.f("ck_project_releases_title_length")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_project_releases_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_project_releases_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_project_releases")),
    )
    op.create_table(
        "project_results",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("place", sa.Integer(), nullable=True),
        sa.Column("award", sa.Text(), nullable=True),
        sa.Column("place_status", sa.Text(), nullable=True),
        sa.Column("place_verified_by", sa.Uuid(), nullable=True),
        sa.Column("place_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("repo_url", sa.Text(), nullable=True),
        sa.Column("demo_url", sa.Text(), nullable=True),
        sa.Column("presentation_url", sa.Text(), nullable=True),
        sa.Column("retrospective_md", sa.Text(), nullable=True),
        sa.Column("member_areas", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("participants", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("auto_closed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("submitted_by", sa.Uuid(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "outcome IN ('finished', 'withdrawn')", name=op.f("ck_project_results_outcome_enum")
        ),
        sa.CheckConstraint(
            "place_status IN ('unverified', 'verified', 'rejected')",
            name=op.f("ck_project_results_place_status_enum"),
        ),
        sa.CheckConstraint(
            "char_length(retrospective_md) <= 5000",
            name=op.f("ck_project_results_retrospective_md_length"),
        ),
        sa.ForeignKeyConstraint(
            ["place_verified_by"],
            ["users.id"],
            name=op.f("fk_project_results_place_verified_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_project_results_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["submitted_by"],
            ["users.id"],
            name=op.f("fk_project_results_submitted_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("project_id", name=op.f("pk_project_results")),
    )
    op.create_table(
        "project_updates",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("author_id", sa.Uuid(), nullable=False),
        sa.Column("body_md", sa.Text(), nullable=False),
        sa.Column("links", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "jsonb_typeof(links) = 'array' AND jsonb_array_length(links) <= 10",
            name=op.f("ck_project_updates_links_length"),
        ),
        sa.CheckConstraint(
            "char_length(body_md) <= 5000", name=op.f("ck_project_updates_body_md_length")
        ),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["users.id"],
            name=op.f("fk_project_updates_author_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_project_updates_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_project_updates")),
    )
    op.create_table(
        "swap_requests",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("from_team_id", sa.Uuid(), nullable=False),
        sa.Column("from_user_id", sa.Uuid(), nullable=False),
        sa.Column("to_team_id", sa.Uuid(), nullable=False),
        sa.Column("to_user_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by", sa.Uuid(), nullable=False),
        sa.Column("to_lead_accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("from_user_accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("to_user_accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'declined', 'expired', 'closed')",
            name=op.f("ck_swap_requests_status_enum"),
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name=op.f("fk_swap_requests_event_id_events"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["from_team_id"],
            ["projects.id"],
            name=op.f("fk_swap_requests_from_team_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["from_user_id"],
            ["users.id"],
            name=op.f("fk_swap_requests_from_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            name=op.f("fk_swap_requests_requested_by_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["to_team_id"],
            ["projects.id"],
            name=op.f("fk_swap_requests_to_team_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["to_user_id"],
            ["users.id"],
            name=op.f("fk_swap_requests_to_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_swap_requests")),
    )
    op.create_table(
        "release_contributions",
        sa.Column("release_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("contributed", sa.Boolean(), nullable=False),
        sa.Column("area_id", sa.Uuid(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["area_id"],
            ["areas.id"],
            name=op.f("fk_release_contributions_area_id_areas"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["release_id"],
            ["project_releases.id"],
            name=op.f("fk_release_contributions_release_id_project_releases"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_release_contributions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("release_id", "user_id", name=op.f("pk_release_contributions")),
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.prevent_area_key_change() CASCADE")
    op.execute("DROP SCHEMA procrastinate CASCADE")
    op.drop_table("release_contributions")
    op.drop_table("swap_requests")
    op.drop_table("project_updates")
    op.drop_table("project_results")
    op.drop_table("project_releases")
    op.drop_table("project_milestones")
    op.drop_index(
        "uq_project_members_project_id_live",
        table_name="project_members",
        postgresql_where=sa.text("role = 'lead' AND left_at IS NULL"),
    )
    op.drop_index(
        "uq_project_members_event_id_user_id_live",
        table_name="project_members",
        postgresql_where=sa.text("left_at IS NULL AND is_team_row"),
    )
    op.drop_table("project_members")
    op.drop_table("project_areas")
    op.drop_table("merge_proposals")
    op.drop_index(
        "uq_lead_offers_project_id_live",
        table_name="lead_offers",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_table("lead_offers")
    op.drop_index(
        "uq_join_requests_project_id_user_id_live",
        table_name="join_requests",
        postgresql_where=sa.text("status = 'pending' AND event_id IS NULL"),
    )
    op.drop_index(
        "uq_join_requests_event_id_user_id_live",
        table_name="join_requests",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_table("join_requests")
    op.drop_index(
        "uq_invites_project_id_user_id_live",
        table_name="invites",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_table("invites")
    op.drop_table("question_options")
    op.drop_table("projects")
    op.drop_table("mentorship_checkins")
    op.drop_table("event_formations")
    op.drop_table("xp_adjustments")
    op.drop_table("review_reports")
    op.drop_table("questions")
    op.drop_index(
        "uq_onboarding_submissions_user_id_live",
        table_name="onboarding_submissions",
        postgresql_where=sa.text("status = 'draft'"),
    )
    op.drop_table("onboarding_submissions")
    op.drop_table("notification_deliveries")
    op.drop_table("mentorships")
    op.drop_table("member_restrictions")
    op.drop_table("lead_fits")
    op.drop_table("help_offers")
    op.drop_table("formation_runs")
    op.drop_table("event_sources")
    op.drop_index("uq_event_source_people_identity", table_name="event_source_people")
    op.drop_table("event_source_people")
    op.drop_table("event_revisions")
    op.drop_table("event_field_locks")
    op.drop_table("event_attendances")
    op.drop_table("event_areas")
    op.drop_index(
        "uq_enrollments_event_id_user_id_live",
        table_name="enrollments",
        postgresql_where=sa.text("status IN ('active', 'placed', 'reserve')"),
    )
    op.drop_table("enrollments")
    op.drop_table("telegram_links")
    op.drop_table("telegram_link_codes")
    op.drop_table("settings")
    op.drop_table("sessions")
    op.drop_table("reviews")
    op.drop_index(
        "uq_quizzes_status_live",
        table_name="quizzes",
        postgresql_where=sa.text("status = 'published'"),
    )
    op.drop_table("quizzes")
    op.drop_table("notifications")
    op.drop_table("notification_settings")
    op.drop_table("mentor_offers")
    op.drop_table("member_reviews")
    op.drop_table("member_metrics")
    op.drop_index(
        "uq_help_requests_learner_id_area_id_live",
        table_name="help_requests",
        postgresql_where=sa.text("status IN ('open', 'direct')"),
    )
    op.drop_table("help_requests")
    op.drop_table("github_snapshots")
    op.drop_index(
        "uq_flags_subject_type_subject_id_live",
        table_name="flags",
        postgresql_where=sa.text("status = 'to_check'"),
    )
    op.drop_table("flags")
    op.drop_table("feed_items")
    op.drop_table("fact_awards")
    op.drop_table("events")
    op.drop_table("cv_files")
    op.drop_index(
        "uq_confirmations_fact_type_fact_id_confirmer_id_live",
        table_name="confirmations",
        postgresql_where=sa.text("withdrawn_at IS NULL"),
        postgresql_nulls_not_distinct=True,
    )
    op.drop_table("confirmations")
    op.drop_table("audit_log")
    op.drop_table("area_scores")
    op.drop_table("area_score_events")
    op.drop_index(
        "uq_admin_alerts_dedupe_key_live",
        table_name="admin_alerts",
        postgresql_where=sa.text("status = 'open'"),
    )
    op.drop_table("admin_alerts")
    op.drop_table("activity_entries")
    op.drop_table("users")
    op.drop_table("review_windows")
    op.drop_table("recommendation_sets")
    op.drop_table("parser_heartbeats")
    op.drop_table("areas")
    op.drop_table("algo_cache")
