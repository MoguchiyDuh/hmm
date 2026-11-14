from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from hmm_contracts import algo as a
from hmm_contracts.common import (
    AttendanceStatus,
    ConfirmationFactType,
    ConfirmationSource,
    FlagStatus,
    FlagSubjectType,
    MentorshipKind,
    MentorshipOutcome,
    RestrictionKind,
    ReviewContext,
    ReviewSubjectType,
)
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.platform.algo import call_algo
from hmm_core.platform.jobs import JobName, Runtime, defer_job
from hmm_core.profiles.github import github_summary
from hmm_core.scores.flags import apply_community_flags
from hmm_core.scores.history import flag_status, pick_flag

WINDOW = timedelta(days=180)


async def _flag_states(
    session: AsyncSession, subject_type: FlagSubjectType, subject_ids: Sequence[UUID]
) -> dict[UUID, FlagStatus | None]:
    rows: dict[UUID, list[models.Flag]] = defaultdict(list)
    if subject_ids:
        for row in await session.scalars(
            select(models.Flag)
            .where(
                models.Flag.subject_type == subject_type.value,
                models.Flag.subject_id.in_(subject_ids),
            )
            .order_by(models.Flag.created_at, models.Flag.id)
        ):
            rows[row.subject_id].append(row)
    return {subject_id: flag_status(pick_flag(rows[subject_id])) for subject_id in subject_ids}


async def _confirmation_contexts(
    session: AsyncSession, rows: Sequence[models.Confirmation]
) -> dict[tuple[str, UUID], UUID]:
    contexts: dict[tuple[str, UUID], UUID] = {}
    attendance_ids = sorted(
        {row.fact_id for row in rows if row.fact_type == ConfirmationFactType.ATTENDANCE}
    )
    if attendance_ids:
        result = await session.execute(
            select(models.EventAttendance.id, models.EventAttendance.event_id).where(
                models.EventAttendance.id.in_(attendance_ids)
            )
        )
        for fact_id, event_id in result:
            contexts[(ConfirmationFactType.ATTENDANCE.value, fact_id)] = event_id
    member_ids = sorted(
        {row.fact_id for row in rows if row.fact_type == ConfirmationFactType.PROJECT_MEMBER}
    )
    if member_ids:
        result = await session.execute(
            select(models.ProjectMember.id, models.ProjectMember.project_id).where(
                models.ProjectMember.id.in_(member_ids)
            )
        )
        for fact_id, project_id in result:
            contexts[(ConfirmationFactType.PROJECT_MEMBER.value, fact_id)] = project_id
    mentorship_ids = sorted(
        {row.fact_id for row in rows if row.fact_type == ConfirmationFactType.MENTORSHIP_SIDE}
    )
    if mentorship_ids:
        for fact_id in await session.scalars(
            select(models.Mentorship.id).where(models.Mentorship.id.in_(mentorship_ids))
        ):
            contexts[(ConfirmationFactType.MENTORSHIP_SIDE.value, fact_id)] = fact_id
    return contexts


async def _load_confirmations(session: AsyncSession, start: datetime) -> list[a.ConfirmationEdge]:
    rows = list(
        await session.scalars(
            select(models.Confirmation)
            .where(models.Confirmation.created_at >= start)
            .order_by(models.Confirmation.created_at, models.Confirmation.id)
        )
    )
    contexts = await _confirmation_contexts(session, rows)
    rows = [row for row in rows if (row.fact_type, row.fact_id) in contexts]
    states = await _flag_states(session, FlagSubjectType.CONFIRMATION, [row.id for row in rows])
    return [
        a.ConfirmationEdge(
            id=row.id,
            confirmer_id=row.confirmer_id,
            subject_user_id=row.subject_user_id,
            fact_type=ConfirmationFactType(row.fact_type),
            fact_id=row.fact_id,
            context_id=contexts[(row.fact_type, row.fact_id)],
            source=ConfirmationSource(row.source),
            created_at=row.created_at,
            ignored=row.ignored,
            withdrawn_at=row.withdrawn_at,
            flag_status=states[row.id],
        )
        for row in rows
    ]


async def _load_mentorships(session: AsyncSession, start: datetime) -> list[a.MentorshipEdge]:
    result = await session.execute(
        select(models.Mentorship, models.Area.key)
        .join(models.Area, models.Area.id == models.Mentorship.area_id)
        .where(
            or_(
                models.Mentorship.started_at >= start,
                models.Mentorship.closed_at >= start,
                models.Mentorship.closed_at.is_(None),
            )
        )
        .order_by(models.Mentorship.started_at, models.Mentorship.id)
    )
    return [
        a.MentorshipEdge(
            id=row.id,
            mentor_id=row.mentor_id,
            learner_id=row.learner_id,
            area=key,
            kind=MentorshipKind(row.kind),
            outcome=MentorshipOutcome(row.outcome) if row.outcome is not None else None,
            started_at=row.started_at,
            closed_at=row.closed_at,
        )
        for row, key in result
    ]


async def _load_reviews(session: AsyncSession, start: datetime) -> list[a.ReviewEdge]:
    rows = list(
        await session.scalars(
            select(models.Review)
            .where(models.Review.created_at >= start)
            .order_by(models.Review.created_at, models.Review.id)
        )
    )
    states = await _flag_states(session, FlagSubjectType.REVIEW, [row.id for row in rows])
    return [
        a.ReviewEdge(
            id=row.id,
            reviewer_id=row.reviewer_id,
            reviewee_id=row.reviewee_id,
            context=ReviewContext(row.context),
            rating=row.rating,
            subject_type=ReviewSubjectType(row.subject_type),
            subject_id=row.subject_id,
            created_at=row.created_at,
            revealed_at=row.revealed_at,
            removed_at=row.removed_at,
            flag_status=states[row.id],
        )
        for row in rows
    ]


async def _load_member_events(
    session: AsyncSession, start: datetime, confirmations: Sequence[a.ConfirmationEdge]
) -> list[a.MemberEventSummary]:
    events = list(
        await session.scalars(
            select(models.Event)
            .where(
                models.Event.origin == "member",
                models.Event.created_at >= start,
                models.Event.created_by.is_not(None),
            )
            .order_by(models.Event.created_at, models.Event.id)
        )
    )
    attendees: dict[UUID, set[UUID]] = defaultdict(set)
    if events:
        result = await session.execute(
            select(models.EventAttendance.event_id, models.EventAttendance.user_id).where(
                models.EventAttendance.event_id.in_([event.id for event in events]),
                models.EventAttendance.status.in_(
                    [AttendanceStatus.GOING.value, AttendanceStatus.ATTENDED.value]
                ),
            )
        )
        for event_id, user_id in result:
            attendees[event_id].add(user_id)
    edges: dict[UUID, list[a.ConfirmationEdge]] = defaultdict(list)
    for edge in confirmations:
        if edge.fact_type == ConfirmationFactType.ATTENDANCE:
            edges[edge.context_id].append(edge)
    summaries: list[a.MemberEventSummary] = []
    for event in events:
        if event.created_by is None:
            continue
        summaries.append(
            a.MemberEventSummary(
                id=event.id,
                creator_id=event.created_by,
                created_at=event.created_at,
                starts_at=event.starts_at,
                ends_at=event.ends_at,
                attendees=sorted(attendees[event.id]),
                confirmations=edges[event.id],
            )
        )
    return summaries


async def _load_accounts(
    session: AsyncSession, user_ids: set[UUID], now: datetime
) -> dict[UUID, a.AccountInfo]:
    ids = sorted(user_ids, key=str)
    if not ids:
        return {}
    users = {
        user.id: user
        for user in await session.scalars(select(models.User).where(models.User.id.in_(ids)))
    }
    metrics: dict[UUID, Decimal] = {
        user_id: xp
        for user_id, xp in await session.execute(
            select(models.MemberMetric.user_id, models.MemberMetric.xp).where(
                models.MemberMetric.user_id.in_(ids)
            )
        )
    }
    adjustments: dict[UUID, Decimal] = {
        user_id: total
        for user_id, total in await session.execute(
            select(models.XpAdjustment.user_id, func.sum(models.XpAdjustment.amount))
            .where(models.XpAdjustment.user_id.in_(ids))
            .group_by(models.XpAdjustment.user_id)
        )
    }
    restrictions: dict[UUID, set[RestrictionKind]] = defaultdict(set)
    result = await session.execute(
        select(models.MemberRestriction.user_id, models.MemberRestriction.kind).where(
            models.MemberRestriction.user_id.in_(ids),
            models.MemberRestriction.lifted_at.is_(None),
            or_(
                models.MemberRestriction.until.is_(None),
                models.MemberRestriction.until > now,
            ),
        )
    )
    for user_id, kind in result:
        restrictions[user_id].add(RestrictionKind(kind))
    snapshots = {
        row.user_id: row
        for row in await session.scalars(
            select(models.GithubSnapshot).where(models.GithubSnapshot.user_id.in_(ids))
        )
    }
    accounts: dict[UUID, a.AccountInfo] = {}
    for user_id in ids:
        user = users.get(user_id)
        if user is None:
            continue
        summary = github_summary(snapshots.get(user_id))
        accounts[user_id] = a.AccountInfo(
            joined_at=user.created_at,
            xp=float(metrics.get(user_id, Decimal(0)) + (adjustments.get(user_id) or Decimal(0))),
            github_account_age=None if summary is None else now - summary.account_created_at,
            restrictions=sorted(restrictions[user_id], key=lambda kind: kind.value),
        )
    return accounts


async def _load_pardoned(session: AsyncSession) -> list[a.PardonedFlag]:
    pardoned: list[a.PardonedFlag] = []
    seen: set[tuple[FlagSubjectType, UUID, str]] = set()
    for row in await session.scalars(
        select(models.Flag)
        .where(models.Flag.status == FlagStatus.PARDONED)
        .order_by(
            models.Flag.subject_type,
            models.Flag.subject_id,
            models.Flag.created_at,
            models.Flag.id,
        )
    ):
        key = (FlagSubjectType(row.subject_type), row.subject_id, row.reason[:200])
        if key in seen:
            continue
        seen.add(key)
        pardoned.append(a.PardonedFlag(subject_type=key[0], subject_id=key[1], reason=key[2]))
    return pardoned


async def build_community_graph(session: AsyncSession, now: datetime) -> a.CommunityGraph:
    start = now - WINDOW
    confirmations = await _load_confirmations(session, start)
    mentorships = await _load_mentorships(session, start)
    reviews = await _load_reviews(session, start)
    member_events = await _load_member_events(session, start, confirmations)
    involved: set[UUID] = set()
    for edge in confirmations:
        involved.add(edge.subject_user_id)
        if edge.confirmer_id is not None:
            involved.add(edge.confirmer_id)
    for edge in mentorships:
        involved.update((edge.mentor_id, edge.learner_id))
    for edge in reviews:
        involved.update((edge.reviewer_id, edge.reviewee_id))
    for event in member_events:
        involved.add(event.creator_id)
        involved.update(event.attendees)
    return a.CommunityGraph(
        confirmations=confirmations,
        mentorships=mentorships,
        reviews=reviews,
        member_events=member_events,
        accounts=await _load_accounts(session, involved, now),
        pardoned=await _load_pardoned(session),
    )


def _validate(graph: a.CommunityGraph, flags: Sequence[a.Flag]) -> None:
    edges = {
        FlagSubjectType.CONFIRMATION: {edge.id for edge in graph.confirmations},
        FlagSubjectType.MENTORSHIP: {edge.id for edge in graph.mentorships},
        FlagSubjectType.REVIEW: {edge.id for edge in graph.reviews},
    }
    for flag in flags:
        if any(user_id not in graph.accounts for user_id in flag.related_user_ids):
            raise ValueError("scan_community flagged an account outside the graph")
        known = edges.get(flag.subject_type)
        if known is not None and flag.subject_id not in known:
            raise ValueError("scan_community flagged an edge outside the graph")


async def scan_community(session: AsyncSession, runtime: Runtime) -> int:
    now = runtime.clock.now()
    graph = await build_community_graph(session, now)
    ctx = await runtime.algo_context(session)
    flags = await call_algo("scan_community", runtime.algorithm.scan_community(ctx, graph))
    _validate(graph, flags)
    rows = await apply_community_flags(
        session, flags, algo_version=runtime.algorithm.version, now=now
    )
    members: set[UUID] = set()
    for row in rows:
        members.add(row.user_id)
        if row.subject_type == FlagSubjectType.RING:
            members.update(row.related_user_ids)
    for user_id in sorted(members):
        await defer_job(session, runtime.jobs, JobName.RECOMPUTE_MEMBER, user_id=str(user_id))
    return len(rows)
