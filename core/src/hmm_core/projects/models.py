from datetime import date, datetime
from uuid import UUID

from hmm_contracts.common import (
    ConfirmKind,
    InviteKind,
    InviteStatus,
    JoinedVia,
    JoinRequestStatus,
    LeadOfferSource,
    LeadOfferStatus,
    LeftReason,
    MemberRole,
    MemberState,
    ProjectOrigin,
    ProjectStatus,
    ProjectVisibility,
    TeamStatus,
    Trust,
)
from pydantic import JsonValue
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from hmm_core.platform.db import Base, Created, Mutable, enum_check


class Project(Mutable, Base):
    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint("char_length(name) <= 100", name="name_length"),
        CheckConstraint("char_length(description_md) <= 5000", name="description_md_length"),
        CheckConstraint("char_length(goal) <= 1000", name="goal_length"),
        CheckConstraint(
            "jsonb_typeof(links) = 'array' AND jsonb_array_length(links) <= 10", name="links_length"
        ),
        enum_check("visibility", ProjectVisibility),
        enum_check("status", ProjectStatus),
        enum_check("team_status", TeamStatus),
        enum_check("origin", ProjectOrigin),
        CheckConstraint("team_status IS NULL OR event_id IS NOT NULL", name="team_event"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    event_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="RESTRICT"), nullable=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description_md: Mapped[str] = mapped_column(Text, nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    links: Mapped[JsonValue] = mapped_column(JSONB, nullable=False)
    visibility: Mapped[str] = mapped_column(Text, nullable=False)
    recruiting: Mapped[JsonValue | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    lead_user_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    team_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    formed_in_run_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("formation_runs.id", ondelete="SET NULL"), nullable=True
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    seat_open_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    seats_closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_lead_action_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_progress_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    recommendation_refresh_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lead_succession_excluded: Mapped[list[UUID]] = mapped_column(
        ARRAY(Uuid), nullable=False, server_default=text("'{}'")
    )


class ProjectArea(Mutable, Base):
    __tablename__ = "project_areas"

    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, primary_key=True
    )
    area_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("areas.id", ondelete="RESTRICT"), nullable=False, primary_key=True
    )


class ProjectMember(Mutable, Base):
    __tablename__ = "project_members"
    __table_args__ = (
        enum_check("role", MemberRole),
        enum_check("state", MemberState),
        enum_check("trust", Trust),
        enum_check("joined_via", JoinedVia),
        CheckConstraint("char_length(proposal_reason) <= 1000", name="proposal_reason_length"),
        enum_check("confirm_kind", ConfirmKind),
        enum_check("left_reason", LeftReason),
        Index(
            "uq_project_members_event_id_user_id_live",
            "event_id",
            "user_id",
            unique=True,
            postgresql_where=text("left_at IS NULL AND is_team_row"),
        ),
        Index(
            "uq_project_members_project_id_live",
            "project_id",
            unique=True,
            postgresql_where=text("role = 'lead' AND left_at IS NULL"),
        ),
        CheckConstraint("NOT is_team_row OR event_id IS NOT NULL", name="team_event"),
        CheckConstraint(
            "state <> 'proposed' OR (confirm_by IS NOT NULL AND confirm_kind IS NOT NULL)",
            name="proposal",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="RESTRICT"), nullable=True
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    area_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("areas.id", ondelete="SET NULL"), nullable=True
    )
    state: Mapped[str] = mapped_column(Text, nullable=False)
    trust: Mapped[str] = mapped_column(Text, nullable=False)
    joined_via: Mapped[str] = mapped_column(Text, nullable=False)
    proposal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirm_by: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirm_kind: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_team_row: Mapped[bool] = mapped_column(Boolean, nullable=False)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    left_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class Invite(Mutable, Base):
    __tablename__ = "invites"
    __table_args__ = (
        enum_check("kind", InviteKind),
        CheckConstraint("char_length(message) <= 1000", name="message_length"),
        enum_check("status", InviteStatus),
        Index(
            "uq_invites_project_id_user_id_live",
            "project_id",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="RESTRICT"), nullable=True
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    invited_by: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    area_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("areas.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    closed_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class JoinRequest(Mutable, Base):
    __tablename__ = "join_requests"
    __table_args__ = (
        CheckConstraint("char_length(note) <= 1000", name="note_length"),
        enum_check("status", JoinRequestStatus),
        Index(
            "uq_join_requests_event_id_user_id_live",
            "event_id",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "uq_join_requests_project_id_user_id_live",
            "project_id",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'pending' AND event_id IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="RESTRICT"), nullable=True
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LeadOffer(Mutable, Base):
    __tablename__ = "lead_offers"
    __table_args__ = (
        enum_check("source", LeadOfferSource),
        CheckConstraint("char_length(reason) <= 1000", name="reason_length"),
        enum_check("status", LeadOfferStatus),
        Index(
            "uq_lead_offers_project_id_live",
            "project_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="RESTRICT"), nullable=True
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    offered_by: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    tier: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProjectRelease(Created, Base):
    __tablename__ = "project_releases"
    __table_args__ = (
        CheckConstraint("char_length(title) <= 200", name="title_length"),
        CheckConstraint("char_length(notes_md) <= 5000", name="notes_md_length"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    notes_md: Mapped[str] = mapped_column(Text, nullable=False)
    repo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    demo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    writeup_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )


class ReleaseContribution(Mutable, Base):
    __tablename__ = "release_contributions"

    release_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("project_releases.id", ondelete="CASCADE"),
        nullable=False,
        primary_key=True,
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, primary_key=True
    )
    contributed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    area_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("areas.id", ondelete="SET NULL"), nullable=True
    )


class ProjectMilestone(Mutable, Base):
    __tablename__ = "project_milestones"
    __table_args__ = (CheckConstraint("char_length(title) <= 200", name="title_length"),)

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    due_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    done_by: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class ProjectUpdate(Mutable, Base):
    __tablename__ = "project_updates"
    __table_args__ = (
        CheckConstraint("char_length(body_md) <= 5000", name="body_md_length"),
        CheckConstraint(
            "jsonb_typeof(links) = 'array' AND jsonb_array_length(links) <= 10", name="links_length"
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    author_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    body_md: Mapped[str] = mapped_column(Text, nullable=False)
    links: Mapped[JsonValue] = mapped_column(JSONB, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
