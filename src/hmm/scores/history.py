from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from typing import Any
from uuid import UUID

from sqlalchemy import Select, Uuid, any_, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm import models
from hmm.algo.models import (
    ActivityEntry,
    CheckinIn,
    ConfirmationIn,
    EventArea,
    EventSnapshot,
    Fact,
    FactContext,
    FactEffort,
    MemberHistory,
    MentorshipContext,
    MentorshipRole,
    MilestoneIn,
    PardonedFlag,
    ProjectResultContext,
    ProjectSnapshot,
    ProjectUpdateIn,
    Recruiting,
    ReleaseContext,
    ReleaseContribution,
)
from hmm.common import (
    ActivityType,
    AreaImportance,
    AttendanceRole,
    AttendanceStatus,
    ConfirmationSource,
    EventFormat,
    EventKind,
    EventOrigin,
    FactType,
    FlagStatus,
    FlagSubjectType,
    MemberRole,
    MemberState,
    MentorshipKind,
    MentorshipOutcome,
    MentorshipStatus,
    PlaceStatus,
    ProjectStatus,
    RestrictionKind,
    ResultOutcome,
    TeamStatus,
    Trust,
)
from hmm.onboarding.baseline import load_onboarding_baseline
from hmm.results.reviews import review_in
from hmm.scores.snapshot import build_member_snapshot

IMPORTANCE_ORDER = (AreaImportance.PRIMARY, AreaImportance.REQUIRED, AreaImportance.NICE)

type FlagKey = tuple[str, UUID]


def empty_effort() -> FactEffort:
    return FactEffort(updates=[], milestones=[], checkins=[], minutes=0)


def empty_context() -> FactContext:
    return FactContext(
        event=None, project=None, result=None, releases=[], mentorship=None, attendance_status=None
    )


def pick_flag(rows: Iterable[models.Flag]) -> models.Flag | None:
    candidates = list(rows)
    for row in candidates:
        if row.status == FlagStatus.TO_CHECK:
            return row
    if not candidates:
        return None
    return max(candidates, key=lambda row: (row.created_at, row.id))


def flag_excluded(row: models.Flag | None) -> bool:
    if row is None or row.status != FlagStatus.PUNISHED:
        return False
    penalty = row.penalty
    return isinstance(penalty, dict) and bool(penalty.get("exclude"))


def flag_status(row: models.Flag | None) -> FlagStatus | None:
    return FlagStatus(row.status) if row is not None else None


def string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


async def grouped[K, T](
    session: AsyncSession, query: Select[T], key: Callable[[T], K]
) -> defaultdict[K, list[T]]:
    rows: defaultdict[K, list[T]] = defaultdict(list)
    for row in await session.scalars(query):
        rows[key(row)].append(row)
    return rows


async def paired(session: AsyncSession, query: Select[UUID, UUID]) -> defaultdict[UUID, list[UUID]]:
    rows: defaultdict[UUID, list[UUID]] = defaultdict(list)
    for left, right in await session.execute(query):
        rows[left].append(right)
    return rows


def confirmed_at(items: list[ConfirmationIn]) -> datetime | None:
    valid = [item.created_at for item in items if item.withdrawn_at is None and not item.ignored]
    return min(valid) if valid else None


@dataclass(slots=True)
class HistoryContext:
    user_id: UUID
    now: datetime
    areas: dict[UUID, models.Area]
    flags: defaultdict[FlagKey, list[models.Flag]]
    confirmations: dict[tuple[str, UUID], list[ConfirmationIn]] = field(default_factory=dict)
    events: dict[UUID, models.Event] = field(default_factory=dict)
    event_areas: defaultdict[UUID, list[models.EventArea]] = field(
        default_factory=lambda: defaultdict(list)
    )

    def area_key(self, area_id: UUID | None) -> str | None:
        area = self.areas.get(area_id) if area_id is not None else None
        return area.key if area is not None else None

    def area_order(self, area_id: UUID) -> tuple[int, str]:
        area = self.areas[area_id]
        return (area.sort, area.key)

    def fact_flag(self, subject_type: FlagSubjectType, subject_id: UUID) -> models.Flag | None:
        return pick_flag(
            row for row in self.flags[(subject_type, subject_id)] if row.user_id == self.user_id
        )

    def subject_flag(self, subject_type: FlagSubjectType, subject_id: UUID) -> models.Flag | None:
        return pick_flag(self.flags[(subject_type, subject_id)])

    def confirmations_for(self, fact_type: FactType, fact_id: UUID) -> list[ConfirmationIn]:
        return self.confirmations.get((fact_type.value, fact_id), [])

    async def load_events(self, session: AsyncSession, event_ids: Iterable[UUID]) -> None:
        missing = [event_id for event_id in dict.fromkeys(event_ids) if event_id not in self.events]
        if not missing:
            return None
        for row in await session.scalars(select(models.Event).where(models.Event.id.in_(missing))):
            self.events[row.id] = row
        for row in await session.scalars(
            select(models.EventArea).where(models.EventArea.event_id.in_(missing))
        ):
            self.event_areas[row.event_id].append(row)
        for event_id in missing:
            self.event_areas[event_id].sort(key=lambda row: self.area_order(row.area_id))

    def event_snapshot(self, event: models.Event) -> EventSnapshot:
        return EventSnapshot(
            id=event.id,
            title=event.title,
            summary=event.summary,
            description_md=event.description_md,
            themes=list(event.themes),
            format=EventFormat(event.format) if event.format is not None else None,
            starts_at=event.starts_at,
            ends_at=event.ends_at,
            timezone=event.timezone,
            areas=[
                EventArea(
                    area=self.areas[row.area_id].key,
                    importance=AreaImportance(row.importance),
                    headcount=row.headcount,
                )
                for row in self.event_areas[event.id]
            ],
            team_size_min=event.team_size_min,
            team_size_max=event.team_size_max,
            difficulty=float(event.difficulty) if event.difficulty is not None else None,
            kind=EventKind(event.kind),
            origin=EventOrigin(event.origin),
            created_by=event.created_by,
        )

    def event_area(self, event_id: UUID) -> str | None:
        rows = self.event_areas[event_id]
        for importance in IMPORTANCE_ORDER:
            for row in rows:
                if row.importance == importance:
                    return self.areas[row.area_id].key
        return None


async def load_confirmations(
    session: AsyncSession, ctx: HistoryContext, rows: list[models.Confirmation]
) -> dict[tuple[str, UUID], list[ConfirmationIn]]:
    confirmer_ids = list(
        dict.fromkeys(row.confirmer_id for row in rows if row.confirmer_id is not None)
    )
    joined = {
        row_id: created_at
        for row_id, created_at in await session.execute(
            select(models.User.id, models.User.created_at).where(models.User.id.in_(confirmer_ids))
        )
    }
    xp = {
        row.user_id: float(row.xp)
        for row in await session.scalars(
            select(models.MemberMetric).where(models.MemberMetric.user_id.in_(confirmer_ids))
        )
    }
    for row_id, amount in await session.execute(
        select(models.XpAdjustment.user_id, func.sum(models.XpAdjustment.amount))
        .where(models.XpAdjustment.user_id.in_(confirmer_ids))
        .group_by(models.XpAdjustment.user_id)
    ):
        xp[row_id] = xp.get(row_id, 0.0) + float(amount)
    restrictions: defaultdict[UUID, set[RestrictionKind]] = defaultdict(set)
    for row in await session.scalars(
        select(models.MemberRestriction).where(
            models.MemberRestriction.user_id.in_(confirmer_ids),
            models.MemberRestriction.active(ctx.now),
        )
    ):
        restrictions[row.user_id].add(RestrictionKind(row.kind))
    output: defaultdict[tuple[str, UUID], list[ConfirmationIn]] = defaultdict(list)
    for row in rows:
        confirmer_id = row.confirmer_id
        created = joined.get(confirmer_id) if confirmer_id is not None else None
        output[(row.fact_type, row.fact_id)].append(
            ConfirmationIn(
                id=row.id,
                confirmer_id=confirmer_id,
                source=ConfirmationSource(row.source),
                confirmer_account_age=ctx.now - created if created is not None else None,
                confirmer_xp=xp.get(confirmer_id, 0.0) if confirmer_id is not None else None,
                confirmer_restrictions=sorted(restrictions[confirmer_id], key=lambda k: k.value)
                if confirmer_id is not None
                else [],
                ignored=row.ignored,
                flag_status=flag_status(ctx.subject_flag(FlagSubjectType.CONFIRMATION, row.id)),
                created_at=row.created_at,
                withdrawn_at=row.withdrawn_at,
            )
        )
    return output


async def attendance_facts(
    session: AsyncSession, ctx: HistoryContext, attendances: list[models.EventAttendance]
) -> list[Fact]:
    await ctx.load_events(session, [row.event_id for row in attendances])
    attendees = await paired(
        session,
        select(models.EventAttendance.event_id, models.EventAttendance.user_id).where(
            models.EventAttendance.event_id.in_([row.event_id for row in attendances]),
            models.EventAttendance.status == AttendanceStatus.ATTENDED,
        ),
    )
    facts: list[Fact] = []
    for attendance in attendances:
        event = ctx.events[attendance.event_id]
        flag = ctx.fact_flag(FlagSubjectType.ATTENDANCE, attendance.id)
        items = ctx.confirmations_for(FactType.ATTENDANCE, attendance.id)
        facts.append(
            Fact(
                type=FactType.ATTENDANCE,
                id=attendance.id,
                role=AttendanceRole(attendance.role),
                area=ctx.event_area(event.id),
                trust=Trust(attendance.trust),
                flag_status=flag_status(flag),
                excluded=flag_excluded(flag),
                proof_links=string_list(attendance.proof_links)[:5],
                note=attendance.note or "",
                created_at=attendance.created_at,
                started_at=event.starts_at,
                ended_at=event.ends_at,
                confirmed_at=confirmed_at(items),
                left_at=None,
                confirmations=items,
                counterparts=sorted(set(attendees[event.id]) - {ctx.user_id}),
                outcome=None,
                effort=empty_effort(),
                context=FactContext(
                    event=ctx.event_snapshot(event),
                    project=None,
                    result=None,
                    releases=[],
                    mentorship=None,
                    attendance_status=AttendanceStatus(attendance.status),
                ),
            )
        )
    return facts


def result_context(result: models.ProjectResult) -> ProjectResultContext:
    participants = result.participants if isinstance(result.participants, dict) else {}
    member_areas = result.member_areas if isinstance(result.member_areas, dict) else {}
    return ProjectResultContext(
        outcome=ResultOutcome(result.outcome),
        place=result.place,
        award=result.award,
        place_status=PlaceStatus(result.place_status) if result.place_status is not None else None,
        participants={UUID(key): bool(value) for key, value in participants.items()},
        member_areas={UUID(key): str(value) for key, value in member_areas.items()},
        repo_url=result.repo_url,
        demo_url=result.demo_url,
        presentation_url=result.presentation_url,
        retrospective_md=result.retrospective_md or "",
        auto_closed=result.auto_closed,
        submitted_at=result.submitted_at,
    )


def project_snapshot(project: models.Project, areas: list[str]) -> ProjectSnapshot:
    return ProjectSnapshot(
        id=project.id,
        name=project.name,
        goal=project.goal,
        description_md=project.description_md,
        areas=areas,
        recruiting=Recruiting.model_validate(project.recruiting)
        if project.recruiting is not None
        else None,
        event_id=project.event_id,
        status=ProjectStatus(project.status),
        team_status=TeamStatus(project.team_status) if project.team_status is not None else None,
        links=string_list(project.links),
    )


async def member_facts(
    session: AsyncSession, ctx: HistoryContext, members: list[models.ProjectMember]
) -> list[Fact]:
    project_ids = list(dict.fromkeys(member.project_id for member in members))
    projects = {
        row.id: row
        for row in await session.scalars(
            select(models.Project).where(models.Project.id.in_(project_ids))
        )
    }
    await ctx.load_events(
        session, [row.event_id for row in projects.values() if row.event_id is not None]
    )
    project_areas = await paired(
        session,
        select(models.ProjectArea.project_id, models.ProjectArea.area_id).where(
            models.ProjectArea.project_id.in_(project_ids)
        ),
    )
    for values in project_areas.values():
        values.sort(key=ctx.area_order)
    project_members = await paired(
        session,
        select(models.ProjectMember.project_id, models.ProjectMember.user_id).where(
            models.ProjectMember.project_id.in_(project_ids),
            models.ProjectMember.state == MemberState.CONFIRMED,
        ),
    )
    results = {
        row.project_id: row
        for row in await session.scalars(
            select(models.ProjectResult).where(models.ProjectResult.project_id.in_(project_ids))
        )
    }
    releases = await grouped(
        session,
        select(models.ProjectRelease)
        .where(models.ProjectRelease.project_id.in_(project_ids))
        .order_by(models.ProjectRelease.created_at, models.ProjectRelease.id),
        lambda row: row.project_id,
    )
    contributions = await grouped(
        session,
        select(models.ReleaseContribution)
        .where(
            models.ReleaseContribution.release_id.in_(
                [release.id for rows in releases.values() for release in rows]
            )
        )
        .order_by(models.ReleaseContribution.user_id),
        lambda row: row.release_id,
    )
    updates = await grouped(
        session,
        select(models.ProjectUpdate)
        .where(
            models.ProjectUpdate.project_id.in_(project_ids),
            models.ProjectUpdate.author_id == ctx.user_id,
        )
        .order_by(models.ProjectUpdate.created_at, models.ProjectUpdate.id),
        lambda row: row.project_id,
    )
    milestones = await grouped(
        session,
        select(models.ProjectMilestone)
        .where(
            models.ProjectMilestone.project_id.in_(project_ids),
            models.ProjectMilestone.done_by == ctx.user_id,
        )
        .order_by(models.ProjectMilestone.position, models.ProjectMilestone.id),
        lambda row: row.project_id,
    )
    facts: list[Fact] = []
    for member in members:
        project = projects[member.project_id]
        result = results.get(project.id)
        member_area = ctx.area_key(member.area_id)
        raw_areas = result.member_areas if result is not None else None
        if member_area is None and isinstance(raw_areas, dict) and str(ctx.user_id) in raw_areas:
            member_area = str(raw_areas[str(ctx.user_id)])
        flag = ctx.fact_flag(FlagSubjectType.PROJECT_MEMBER, member.id)
        items = ctx.confirmations_for(FactType.PROJECT_MEMBER, member.id)
        facts.append(
            Fact(
                type=FactType.PROJECT_MEMBER,
                id=member.id,
                role=MemberRole(member.role),
                area=member_area,
                trust=Trust(member.trust),
                flag_status=flag_status(flag),
                excluded=flag_excluded(flag),
                proof_links=[],
                note="",
                created_at=member.created_at,
                started_at=member.joined_at,
                ended_at=None,
                confirmed_at=member.confirmed_at
                if member.confirmed_at is not None
                else confirmed_at(items),
                left_at=member.left_at,
                confirmations=items,
                counterparts=sorted(set(project_members[project.id]) - {ctx.user_id}),
                outcome=ResultOutcome(result.outcome)
                if result is not None
                else ProjectStatus(project.status),
                effort=FactEffort(
                    updates=[
                        ProjectUpdateIn(
                            id=row.id,
                            author_id=row.author_id,
                            body_md=row.body_md,
                            links=string_list(row.links),
                            created_at=row.created_at,
                            deleted_at=row.deleted_at,
                        )
                        for row in updates[project.id]
                    ],
                    milestones=[
                        MilestoneIn(
                            id=row.id,
                            title=row.title,
                            description=row.description,
                            due_on=row.due_on,
                            done_by=row.done_by,
                            done_at=row.done_at,
                        )
                        for row in milestones[project.id]
                    ],
                    checkins=[],
                    minutes=0,
                ),
                context=FactContext(
                    event=ctx.event_snapshot(ctx.events[project.event_id])
                    if project.event_id is not None
                    else None,
                    project=project_snapshot(
                        project, [ctx.areas[area_id].key for area_id in project_areas[project.id]]
                    ),
                    result=result_context(result) if result is not None else None,
                    releases=[
                        ReleaseContext(
                            id=release.id,
                            title=release.title,
                            notes_md=release.notes_md,
                            repo_url=release.repo_url,
                            demo_url=release.demo_url,
                            writeup_url=release.writeup_url,
                            created_at=release.created_at,
                            contributions=[
                                ReleaseContribution(
                                    user_id=row.user_id,
                                    contributed=row.contributed,
                                    area=ctx.area_key(row.area_id),
                                )
                                for row in contributions[release.id]
                            ],
                        )
                        for release in releases[project.id]
                    ],
                    mentorship=None,
                    attendance_status=None,
                ),
            )
        )
    return facts


async def mentorship_facts(
    session: AsyncSession, ctx: HistoryContext, mentorships: list[models.Mentorship]
) -> list[Fact]:
    checkins = await grouped(
        session,
        select(models.MentorshipCheckin)
        .where(models.MentorshipCheckin.mentorship_id.in_([row.id for row in mentorships]))
        .order_by(models.MentorshipCheckin.occurred_on, models.MentorshipCheckin.id),
        lambda row: row.mentorship_id,
    )
    learner_scores = {
        (row.user_id, row.area_id): float(
            row.override if row.override is not None else row.calculated
        )
        for row in await session.scalars(
            select(models.AreaScore).where(
                models.AreaScore.user_id.in_({row.learner_id for row in mentorships})
            )
        )
    }
    facts: list[Fact] = []
    for mentorship in mentorships:
        pair = {mentorship.mentor_id, mentorship.learner_id}
        earlier = sum(
            1
            for other in mentorships
            if {other.mentor_id, other.learner_id} == pair
            and other.started_at < mentorship.started_at
        )
        rows = checkins[mentorship.id]
        own = [row for row in rows if row.author_id == ctx.user_id]
        study_pair = mentorship.kind == MentorshipKind.STUDY_PAIR
        if mentorship.mentor_id == ctx.user_id:
            role = MentorshipRole.PEER if study_pair else MentorshipRole.MENTOR
            trust = Trust(mentorship.mentor_trust)
            counterpart = mentorship.learner_id
        else:
            role = MentorshipRole.PEER if study_pair else MentorshipRole.MENTEE
            trust = Trust(mentorship.learner_trust)
            counterpart = mentorship.mentor_id
        outcome = MentorshipOutcome(mentorship.outcome) if mentorship.outcome is not None else None
        key = ctx.areas[mentorship.area_id].key
        flag = ctx.fact_flag(FlagSubjectType.MENTORSHIP, mentorship.id)
        items = ctx.confirmations_for(FactType.MENTORSHIP_SIDE, mentorship.id)
        facts.append(
            Fact(
                type=FactType.MENTORSHIP_SIDE,
                id=mentorship.id,
                role=role,
                area=key,
                trust=trust,
                flag_status=flag_status(flag),
                excluded=flag_excluded(flag),
                proof_links=[],
                note="",
                created_at=mentorship.created_at,
                started_at=mentorship.started_at,
                ended_at=mentorship.closed_at,
                confirmed_at=confirmed_at(items),
                left_at=None,
                confirmations=items,
                counterparts=[counterpart],
                outcome=outcome,
                effort=FactEffort(
                    updates=[],
                    milestones=[],
                    checkins=[
                        CheckinIn(
                            id=row.id,
                            author_id=row.author_id,
                            note=row.note,
                            occurred_on=row.occurred_on,
                            minutes=row.minutes,
                        )
                        for row in own
                    ],
                    minutes=sum(row.minutes or 0 for row in own),
                ),
                context=FactContext(
                    event=None,
                    project=None,
                    result=None,
                    releases=[],
                    mentorship=MentorshipContext(
                        id=mentorship.id,
                        kind=MentorshipKind(mentorship.kind),
                        status=MentorshipStatus(mentorship.status),
                        mentor_id=mentorship.mentor_id,
                        learner_id=mentorship.learner_id,
                        area=key,
                        mentor_score_at_start=float(mentorship.mentor_score_at_start),
                        learner_score_at_start=float(mentorship.learner_score_at_start),
                        learner_score_now=learner_scores.get(
                            (mentorship.learner_id, mentorship.area_id), 0.0
                        ),
                        started_at=mentorship.started_at,
                        closed_at=mentorship.closed_at,
                        duration=(mentorship.closed_at or ctx.now) - mentorship.started_at,
                        mentor_checkin_count=sum(
                            1 for row in rows if row.author_id == mentorship.mentor_id
                        ),
                        learner_checkin_count=sum(
                            1 for row in rows if row.author_id == mentorship.learner_id
                        ),
                        earlier_pair_mentorships=earlier,
                        outcome=outcome,
                        summary=mentorship.summary,
                        mentor_acknowledged_at=mentorship.mentor_acknowledged_at,
                        endorsement=mentorship.endorsement,
                    ),
                    attendance_status=None,
                ),
            )
        )
    return facts


def activity_facts(
    ctx: HistoryContext, entries: list[models.ActivityEntry]
) -> tuple[list[Fact], list[ActivityEntry]]:
    facts: list[Fact] = []
    activity: list[ActivityEntry] = []
    for entry in entries:
        flag = ctx.fact_flag(FlagSubjectType.ACTIVITY_ENTRY, entry.id)
        excluded = entry.deleted_at is not None or flag_excluded(flag)
        key = ctx.area_key(entry.area_id)
        facts.append(
            Fact(
                type=FactType.ACTIVITY_ENTRY,
                id=entry.id,
                role=None,
                area=key,
                trust=Trust.SELF,
                flag_status=flag_status(flag),
                excluded=excluded,
                proof_links=[],
                note="",
                created_at=entry.created_at,
                started_at=datetime.combine(entry.occurred_on, time(), tzinfo=UTC),
                ended_at=None,
                confirmed_at=None,
                left_at=None,
                confirmations=[],
                counterparts=[],
                outcome=None,
                effort=empty_effort(),
                context=empty_context(),
            )
        )
        activity.append(
            ActivityEntry(
                id=entry.id,
                type=ActivityType(entry.type),
                title=entry.title,
                description_md=entry.description_md,
                links=string_list(entry.links),
                area=key,
                occurred_on=entry.occurred_on,
                created_at=entry.created_at,
                deleted_at=entry.deleted_at,
                flag_status=flag_status(flag),
                excluded=excluded,
            )
        )
    return facts, activity


async def pardoned_flags(session: AsyncSession, user_id: UUID) -> list[PardonedFlag]:
    return sorted(
        (
            PardonedFlag(
                subject_type=FlagSubjectType(row.subject_type),
                subject_id=row.subject_id,
                reason=row.reason[:200],
            )
            for row in await session.scalars(
                select(models.Flag).where(
                    models.Flag.status == FlagStatus.PARDONED,
                    or_(
                        models.Flag.user_id == user_id,
                        literal(user_id, Uuid) == any_(models.Flag.related_user_ids),
                    ),
                )
            )
        ),
        key=lambda row: (row.subject_type.value, str(row.subject_id), row.reason),
    )


async def load_member_history(session: AsyncSession, user_id: UUID, now: datetime) -> MemberHistory:
    snapshot = await build_member_snapshot(session, user_id)
    onboarding = await load_onboarding_baseline(session, user_id)
    attendances = list(
        await session.scalars(
            select(models.EventAttendance).where(
                models.EventAttendance.user_id == user_id,
                models.EventAttendance.status == AttendanceStatus.ATTENDED,
            )
        )
    )
    members = list(
        await session.scalars(
            select(models.ProjectMember).where(
                models.ProjectMember.user_id == user_id,
                models.ProjectMember.state == MemberState.CONFIRMED,
            )
        )
    )
    mentorships = list(
        await session.scalars(
            select(models.Mentorship).where(
                or_(models.Mentorship.mentor_id == user_id, models.Mentorship.learner_id == user_id)
            )
        )
    )
    entries = list(
        await session.scalars(
            select(models.ActivityEntry)
            .where(models.ActivityEntry.user_id == user_id)
            .order_by(models.ActivityEntry.created_at, models.ActivityEntry.id)
        )
    )
    reviews = list(
        await session.scalars(
            select(models.Review)
            .where(
                models.Review.reviewee_id == user_id,
                models.Review.revealed_at.is_not(None),
                models.Review.removed_at.is_(None),
            )
            .order_by(models.Review.created_at, models.Review.id)
        )
    )
    confirmations = list(
        await session.scalars(
            select(models.Confirmation)
            .where(models.Confirmation.subject_user_id == user_id)
            .order_by(models.Confirmation.created_at, models.Confirmation.id)
        )
    )
    subject_ids = list(
        dict.fromkeys(
            row.id
            for rows in (attendances, members, mentorships, entries, confirmations, reviews)
            for row in rows
        )
    )
    ctx = HistoryContext(
        user_id=user_id,
        now=now,
        areas={area.id: area for area in await session.scalars(select(models.Area))},
        flags=await grouped(
            session,
            select(models.Flag).where(models.Flag.subject_id.in_(subject_ids)),
            lambda row: (row.subject_type, row.subject_id),
        ),
    )
    ctx.confirmations = await load_confirmations(session, ctx, confirmations)
    activity, activity_entries = activity_facts(ctx, entries)
    facts = [
        *await attendance_facts(session, ctx, attendances),
        *await member_facts(session, ctx, members),
        *await mentorship_facts(session, ctx, mentorships),
        *activity,
    ]
    facts.sort(key=lambda fact: (fact.started_at, fact.type.value, str(fact.id)))
    adjustments_total = await session.scalar(
        select(func.coalesce(func.sum(models.XpAdjustment.amount), 0)).where(
            models.XpAdjustment.user_id == user_id
        )
    )
    return MemberHistory.model_validate(
        {
            **snapshot.model_dump(),
            "onboarding": onboarding,
            "facts": facts,
            "activity_entries": activity_entries,
            "reviews_received": [
                review_in(row, flag_status(ctx.subject_flag(FlagSubjectType.REVIEW, row.id)))
                for row in reviews
            ],
            "xp_adjustments_total": float(adjustments_total or 0),
            "pardoned": await pardoned_flags(session, user_id),
        }
    )
