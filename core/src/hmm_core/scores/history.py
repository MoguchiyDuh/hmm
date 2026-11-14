from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime, time
from typing import Any
from uuid import UUID

from hmm_contracts.algo import (
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
    ReviewIn,
)
from hmm_contracts.common import (
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
    ReviewContext,
    ReviewSubjectType,
    TeamStatus,
    Trust,
)
from sqlalchemy import Uuid, any_, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.onboarding.baseline import load_onboarding_baseline
from hmm_core.scores.snapshot import build_member_snapshot

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


async def load_member_history(session: AsyncSession, user_id: UUID, now: datetime) -> MemberHistory:
    snapshot = await build_member_snapshot(session, user_id, now)
    onboarding = await load_onboarding_baseline(session, user_id)
    areas = {area.id: area for area in await session.scalars(select(models.Area))}

    def area_key(area_id: UUID | None) -> str | None:
        area = areas.get(area_id) if area_id is not None else None
        return area.key if area is not None else None

    def area_order(area_id: UUID) -> tuple[int, str]:
        area = areas[area_id]
        return (area.sort, area.key)

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
    confirmations: dict[tuple[str, UUID], list[models.Confirmation]] = defaultdict(list)
    for row in await session.scalars(
        select(models.Confirmation)
        .where(models.Confirmation.subject_user_id == user_id)
        .order_by(models.Confirmation.created_at, models.Confirmation.id)
    ):
        confirmations[(row.fact_type, row.fact_id)].append(row)
    confirmation_rows = [row for rows in confirmations.values() for row in rows]

    project_ids = list(dict.fromkeys(member.project_id for member in members))
    projects = {
        row.id: row
        for row in await session.scalars(
            select(models.Project).where(models.Project.id.in_(project_ids))
        )
    }
    event_ids = list(
        dict.fromkeys(
            [attendance.event_id for attendance in attendances]
            + [row.event_id for row in projects.values() if row.event_id is not None]
        )
    )
    events = {
        row.id: row
        for row in await session.scalars(select(models.Event).where(models.Event.id.in_(event_ids)))
    }
    event_areas: dict[UUID, list[models.EventArea]] = defaultdict(list)
    for row in await session.scalars(
        select(models.EventArea).where(models.EventArea.event_id.in_(event_ids))
    ):
        event_areas[row.event_id].append(row)
    for rows in event_areas.values():
        rows.sort(key=lambda row: area_order(row.area_id))
    attendees: dict[UUID, set[UUID]] = defaultdict(set)
    for event_id, attendee_id in await session.execute(
        select(models.EventAttendance.event_id, models.EventAttendance.user_id).where(
            models.EventAttendance.event_id.in_([row.event_id for row in attendances]),
            models.EventAttendance.status == AttendanceStatus.ATTENDED,
        )
    ):
        attendees[event_id].add(attendee_id)
    project_areas: dict[UUID, list[UUID]] = defaultdict(list)
    for project_id, project_area_id in await session.execute(
        select(models.ProjectArea.project_id, models.ProjectArea.area_id).where(
            models.ProjectArea.project_id.in_(project_ids)
        )
    ):
        project_areas[project_id].append(project_area_id)
    for values in project_areas.values():
        values.sort(key=area_order)
    project_members: dict[UUID, set[UUID]] = defaultdict(set)
    for project_id, member_id in await session.execute(
        select(models.ProjectMember.project_id, models.ProjectMember.user_id).where(
            models.ProjectMember.project_id.in_(project_ids),
            models.ProjectMember.state == MemberState.CONFIRMED,
        )
    ):
        project_members[project_id].add(member_id)
    results = {
        row.project_id: row
        for row in await session.scalars(
            select(models.ProjectResult).where(models.ProjectResult.project_id.in_(project_ids))
        )
    }
    releases: dict[UUID, list[models.ProjectRelease]] = defaultdict(list)
    for row in await session.scalars(
        select(models.ProjectRelease)
        .where(models.ProjectRelease.project_id.in_(project_ids))
        .order_by(models.ProjectRelease.created_at, models.ProjectRelease.id)
    ):
        releases[row.project_id].append(row)
    contributions: dict[UUID, list[models.ReleaseContribution]] = defaultdict(list)
    for row in await session.scalars(
        select(models.ReleaseContribution)
        .where(
            models.ReleaseContribution.release_id.in_(
                [release.id for rows in releases.values() for release in rows]
            )
        )
        .order_by(models.ReleaseContribution.user_id)
    ):
        contributions[row.release_id].append(row)
    updates: dict[UUID, list[models.ProjectUpdate]] = defaultdict(list)
    for row in await session.scalars(
        select(models.ProjectUpdate)
        .where(
            models.ProjectUpdate.project_id.in_(project_ids),
            models.ProjectUpdate.author_id == user_id,
        )
        .order_by(models.ProjectUpdate.created_at, models.ProjectUpdate.id)
    ):
        updates[row.project_id].append(row)
    milestones: dict[UUID, list[models.ProjectMilestone]] = defaultdict(list)
    for row in await session.scalars(
        select(models.ProjectMilestone)
        .where(
            models.ProjectMilestone.project_id.in_(project_ids),
            models.ProjectMilestone.done_by == user_id,
        )
        .order_by(models.ProjectMilestone.position, models.ProjectMilestone.id)
    ):
        milestones[row.project_id].append(row)

    mentorship_ids = [row.id for row in mentorships]
    checkins: dict[UUID, list[models.MentorshipCheckin]] = defaultdict(list)
    for row in await session.scalars(
        select(models.MentorshipCheckin)
        .where(models.MentorshipCheckin.mentorship_id.in_(mentorship_ids))
        .order_by(models.MentorshipCheckin.occurred_on, models.MentorshipCheckin.id)
    ):
        checkins[row.mentorship_id].append(row)
    learner_scores: dict[tuple[UUID, UUID], float] = {}
    for row in await session.scalars(
        select(models.AreaScore).where(
            models.AreaScore.user_id.in_({row.learner_id for row in mentorships})
        )
    ):
        effective = row.override if row.override is not None else row.calculated
        learner_scores[(row.user_id, row.area_id)] = float(effective)

    confirmer_ids = list(
        dict.fromkeys(row.confirmer_id for row in confirmation_rows if row.confirmer_id is not None)
    )
    confirmer_joined = {
        row_id: created_at
        for row_id, created_at in await session.execute(
            select(models.User.id, models.User.created_at).where(models.User.id.in_(confirmer_ids))
        )
    }
    confirmer_metrics = {
        row.user_id: float(row.xp)
        for row in await session.scalars(
            select(models.MemberMetric).where(models.MemberMetric.user_id.in_(confirmer_ids))
        )
    }
    confirmer_adjustments = {
        row_id: float(amount)
        for row_id, amount in await session.execute(
            select(models.XpAdjustment.user_id, func.sum(models.XpAdjustment.amount))
            .where(models.XpAdjustment.user_id.in_(confirmer_ids))
            .group_by(models.XpAdjustment.user_id)
        )
    }
    confirmer_restrictions: dict[UUID, set[RestrictionKind]] = defaultdict(set)
    for row in await session.scalars(
        select(models.MemberRestriction).where(
            models.MemberRestriction.user_id.in_(confirmer_ids),
            models.MemberRestriction.lifted_at.is_(None),
            or_(models.MemberRestriction.until.is_(None), models.MemberRestriction.until > now),
        )
    ):
        confirmer_restrictions[row.user_id].add(RestrictionKind(row.kind))

    subject_ids = list(
        dict.fromkeys(
            [row.id for row in attendances]
            + [row.id for row in members]
            + mentorship_ids
            + [row.id for row in entries]
            + [row.id for row in confirmation_rows]
            + [row.id for row in reviews]
        )
    )
    flags: dict[FlagKey, list[models.Flag]] = defaultdict(list)
    for row in await session.scalars(
        select(models.Flag).where(models.Flag.subject_id.in_(subject_ids))
    ):
        flags[(row.subject_type, row.subject_id)].append(row)

    def fact_flag(subject_type: FlagSubjectType, subject_id: UUID) -> models.Flag | None:
        return pick_flag(row for row in flags[(subject_type, subject_id)] if row.user_id == user_id)

    def subject_flag(subject_type: FlagSubjectType, subject_id: UUID) -> models.Flag | None:
        return pick_flag(flags[(subject_type, subject_id)])

    def confirmations_for(fact_type: FactType, fact_id: UUID) -> list[ConfirmationIn]:
        output: list[ConfirmationIn] = []
        for row in confirmations[(fact_type.value, fact_id)]:
            confirmer_id = row.confirmer_id
            if confirmer_id is None:
                account_age = None
                xp = None
                restrictions: list[RestrictionKind] = []
            else:
                joined = confirmer_joined.get(confirmer_id)
                account_age = now - joined if joined is not None else None
                xp = confirmer_metrics.get(confirmer_id, 0.0) + confirmer_adjustments.get(
                    confirmer_id, 0.0
                )
                restrictions = sorted(
                    confirmer_restrictions[confirmer_id], key=lambda kind: kind.value
                )
            output.append(
                ConfirmationIn(
                    id=row.id,
                    confirmer_id=confirmer_id,
                    source=ConfirmationSource(row.source),
                    confirmer_account_age=account_age,
                    confirmer_xp=xp,
                    confirmer_restrictions=restrictions,
                    ignored=row.ignored,
                    flag_status=flag_status(subject_flag(FlagSubjectType.CONFIRMATION, row.id)),
                    created_at=row.created_at,
                    withdrawn_at=row.withdrawn_at,
                )
            )
        return output

    def confirmed_at(items: list[ConfirmationIn]) -> datetime | None:
        valid = [
            item.created_at for item in items if item.withdrawn_at is None and not item.ignored
        ]
        return min(valid) if valid else None

    def event_snapshot(event: models.Event) -> EventSnapshot:
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
                    area=areas[row.area_id].key,
                    importance=AreaImportance(row.importance),
                    headcount=row.headcount,
                )
                for row in event_areas[event.id]
            ],
            team_size_min=event.team_size_min,
            team_size_max=event.team_size_max,
            difficulty=float(event.difficulty) if event.difficulty is not None else None,
            kind=EventKind(event.kind),
            origin=EventOrigin(event.origin),
            created_by=event.created_by,
        )

    def event_area(event_id: UUID) -> str | None:
        rows = event_areas[event_id]
        for importance in IMPORTANCE_ORDER:
            for row in rows:
                if row.importance == importance:
                    return areas[row.area_id].key
        return None

    facts: list[Fact] = []
    for attendance in attendances:
        event = events[attendance.event_id]
        flag = fact_flag(FlagSubjectType.ATTENDANCE, attendance.id)
        items = confirmations_for(FactType.ATTENDANCE, attendance.id)
        facts.append(
            Fact(
                type=FactType.ATTENDANCE,
                id=attendance.id,
                role=AttendanceRole(attendance.role),
                area=event_area(event.id),
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
                counterparts=sorted(attendees[event.id] - {user_id}),
                outcome=None,
                effort=empty_effort(),
                context=FactContext(
                    event=event_snapshot(event),
                    project=None,
                    result=None,
                    releases=[],
                    mentorship=None,
                    attendance_status=AttendanceStatus(attendance.status),
                ),
            )
        )

    for member in members:
        project = projects[member.project_id]
        result = results.get(project.id)
        result_context: ProjectResultContext | None = None
        member_areas: dict[str, Any] = {}
        if result is not None:
            participants = result.participants if isinstance(result.participants, dict) else {}
            raw_areas = result.member_areas if isinstance(result.member_areas, dict) else {}
            member_areas = raw_areas
            result_context = ProjectResultContext(
                outcome=ResultOutcome(result.outcome),
                place=result.place,
                award=result.award,
                place_status=PlaceStatus(result.place_status)
                if result.place_status is not None
                else None,
                participants={UUID(key): bool(value) for key, value in participants.items()},
                member_areas={UUID(key): str(value) for key, value in raw_areas.items()},
                repo_url=result.repo_url,
                demo_url=result.demo_url,
                presentation_url=result.presentation_url,
                retrospective_md=result.retrospective_md or "",
                auto_closed=result.auto_closed,
                submitted_at=result.submitted_at,
            )
        member_area = area_key(member.area_id)
        if member_area is None and str(user_id) in member_areas:
            member_area = str(member_areas[str(user_id)])
        flag = fact_flag(FlagSubjectType.PROJECT_MEMBER, member.id)
        items = confirmations_for(FactType.PROJECT_MEMBER, member.id)
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
                counterparts=sorted(project_members[project.id] - {user_id}),
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
                    event=event_snapshot(events[project.event_id])
                    if project.event_id is not None
                    else None,
                    project=ProjectSnapshot(
                        id=project.id,
                        name=project.name,
                        goal=project.goal,
                        description_md=project.description_md,
                        areas=[areas[area_id].key for area_id in project_areas[project.id]],
                        recruiting=Recruiting.model_validate(project.recruiting)
                        if project.recruiting is not None
                        else None,
                        event_id=project.event_id,
                        status=ProjectStatus(project.status),
                        team_status=TeamStatus(project.team_status)
                        if project.team_status is not None
                        else None,
                        links=string_list(project.links),
                    ),
                    result=result_context,
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
                                    area=area_key(row.area_id),
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

    for mentorship in mentorships:
        pair = {mentorship.mentor_id, mentorship.learner_id}
        earlier = sum(
            1
            for other in mentorships
            if {other.mentor_id, other.learner_id} == pair
            and other.started_at < mentorship.started_at
        )
        rows = checkins[mentorship.id]
        own = [row for row in rows if row.author_id == user_id]
        study_pair = mentorship.kind == MentorshipKind.STUDY_PAIR
        is_mentor = mentorship.mentor_id == user_id
        if is_mentor:
            role = MentorshipRole.PEER if study_pair else MentorshipRole.MENTOR
            trust = Trust(mentorship.mentor_trust)
            counterpart = mentorship.learner_id
        else:
            role = MentorshipRole.PEER if study_pair else MentorshipRole.MENTEE
            trust = Trust(mentorship.learner_trust)
            counterpart = mentorship.mentor_id
        outcome = MentorshipOutcome(mentorship.outcome) if mentorship.outcome is not None else None
        key = areas[mentorship.area_id].key
        flag = fact_flag(FlagSubjectType.MENTORSHIP, mentorship.id)
        items = confirmations_for(FactType.MENTORSHIP_SIDE, mentorship.id)
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
                        duration=(mentorship.closed_at or now) - mentorship.started_at,
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

    activity_entries: list[ActivityEntry] = []
    for entry in entries:
        flag = fact_flag(FlagSubjectType.ACTIVITY_ENTRY, entry.id)
        excluded = entry.deleted_at is not None or flag_excluded(flag)
        key = area_key(entry.area_id)
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
        activity_entries.append(
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
    facts.sort(key=lambda fact: (fact.started_at, fact.type.value, str(fact.id)))

    reviews_received = [
        ReviewIn(
            id=row.id,
            reviewer_id=row.reviewer_id,
            reviewee_id=row.reviewee_id,
            context=ReviewContext(row.context),
            rating=row.rating,
            subject_type=ReviewSubjectType(row.subject_type),
            subject_id=row.subject_id,
            comment=row.comment,
            created_at=row.created_at,
            revealed_at=row.revealed_at,
            removed_at=row.removed_at,
            flag_status=flag_status(subject_flag(FlagSubjectType.REVIEW, row.id)),
        )
        for row in reviews
    ]
    adjustments_total = await session.scalar(
        select(func.coalesce(func.sum(models.XpAdjustment.amount), 0)).where(
            models.XpAdjustment.user_id == user_id
        )
    )
    pardoned = sorted(
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
    return MemberHistory.model_validate(
        {
            **snapshot.model_dump(),
            "onboarding": onboarding,
            "facts": facts,
            "activity_entries": activity_entries,
            "reviews_received": reviews_received,
            "xp_adjustments_total": float(adjustments_total or 0),
            "pardoned": pardoned,
        }
    )
