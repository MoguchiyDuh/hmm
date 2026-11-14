from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

import procrastinate
from hmm_contracts.common import ConfirmationFactType, ConfirmationSource, ErrorCode, Trust
from sqlalchemy import ARRAY, Uuid, bindparam, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from hmm_core import models
from hmm_core.feed import schemas
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.jobs import JobName, defer_job
from hmm_core.platform.settings import load_settings


@dataclass(frozen=True, slots=True)
class ConfirmationCandidate:
    fact_type: ConfirmationFactType
    fact_id: UUID
    subject_user_id: UUID
    source: ConfirmationSource
    subject_type: str
    subject_id: UUID
    title: str


@dataclass(frozen=True, slots=True)
class _Fact:
    row: models.EventAttendance | models.ProjectMember | models.Mentorship
    role: str
    trust_column: str


async def _resolve_fact(
    session: AsyncSession,
    fact_type: ConfirmationFactType,
    fact_id: UUID,
    subject_user_id: UUID,
    *,
    lock: bool = False,
) -> _Fact:
    if fact_type == ConfirmationFactType.ATTENDANCE:
        statement = select(models.EventAttendance).where(models.EventAttendance.id == fact_id)
        row = await session.scalar(statement.with_for_update() if lock else statement)
        if row is None:
            raise ProblemError(ErrorCode.NOT_FOUND)
        if row.user_id != subject_user_id:
            raise ValueError("Attendance subject does not match")
        return _Fact(row, row.role, "trust")
    if fact_type == ConfirmationFactType.PROJECT_MEMBER:
        statement = select(models.ProjectMember).where(models.ProjectMember.id == fact_id)
        row = await session.scalar(statement.with_for_update() if lock else statement)
        if row is None:
            raise ProblemError(ErrorCode.NOT_FOUND)
        if row.user_id != subject_user_id:
            raise ValueError("Project member subject does not match")
        return _Fact(row, row.role, "trust")
    if fact_type == ConfirmationFactType.MENTORSHIP_SIDE:
        statement = select(models.Mentorship).where(models.Mentorship.id == fact_id)
        row = await session.scalar(statement.with_for_update() if lock else statement)
        if row is None:
            raise ProblemError(ErrorCode.NOT_FOUND)
        if subject_user_id == row.mentor_id:
            return _Fact(row, "mentor", "mentor_trust")
        if subject_user_id == row.learner_id:
            return _Fact(row, "learner", "learner_trust")
        raise ValueError("Mentorship subject does not match")
    raise ValueError("Unknown confirmation fact type")


def _source_trust(
    fact_type: ConfirmationFactType, role: str, source: ConfirmationSource
) -> Trust | None:
    if source in {ConfirmationSource.ADMIN, ConfirmationSource.SYSTEM}:
        return Trust.TRUSTED
    if source == ConfirmationSource.PEER:
        return Trust.PEER
    if fact_type == ConfirmationFactType.ATTENDANCE and source in {
        ConfirmationSource.ORGANIZER,
        ConfirmationSource.HOST,
    }:
        return Trust.PEER
    if (
        fact_type == ConfirmationFactType.PROJECT_MEMBER
        and role == "member"
        and source == ConfirmationSource.LEAD
    ):
        return Trust.TRUSTED
    if fact_type == ConfirmationFactType.MENTORSHIP_SIDE and (
        (role == "mentor" and source == ConfirmationSource.LEARNER)
        or (role == "learner" and source == ConfirmationSource.MENTOR)
    ):
        return Trust.TRUSTED
    return None


async def _ignore_reasons(
    session: AsyncSession,
    confirmer_ids: set[UUID],
    confirmation_ids: set[UUID],
    now: datetime,
) -> tuple[set[UUID], set[UUID]]:
    if not confirmer_ids:
        return set(), set()
    ignored_users = set(
        await session.scalars(
            select(models.User.id).where(
                models.User.id.in_(confirmer_ids), models.User.banned_at.is_not(None)
            )
        )
    )
    ignored_users.update(
        await session.scalars(
            select(models.MemberRestriction.user_id)
            .where(
                models.MemberRestriction.user_id.in_(confirmer_ids),
                models.MemberRestriction.kind == "confirming",
                models.MemberRestriction.lifted_at.is_(None),
                or_(models.MemberRestriction.until.is_(None), models.MemberRestriction.until > now),
            )
            .distinct()
        )
    )
    flags = await session.scalars(
        select(models.Flag).where(
            models.Flag.status == "to_check",
            or_(
                models.Flag.user_id.in_(confirmer_ids),
                (models.Flag.subject_type == "ring")
                & models.Flag.related_user_ids.op("&&")(
                    bindparam("confirmer_ids", list(confirmer_ids), type_=ARRAY(Uuid))
                ),
                (models.Flag.subject_type == "confirmation")
                & models.Flag.subject_id.in_(confirmation_ids),
            ),
        )
    )
    ignored_confirmations: set[UUID] = set()
    for flag in flags:
        if flag.user_id in confirmer_ids:
            ignored_users.add(flag.user_id)
        if flag.subject_type == "ring":
            ignored_users.update(confirmer_ids.intersection(flag.related_user_ids))
        if flag.subject_type == "confirmation" and flag.subject_id in confirmation_ids:
            ignored_confirmations.add(flag.subject_id)
    return ignored_users, ignored_confirmations


async def _ignored(
    session: AsyncSession, confirmer_id: UUID | None, confirmation_id: UUID | None, now: datetime
) -> bool:
    if confirmer_id is None:
        return False
    users, confirmations = await _ignore_reasons(
        session, {confirmer_id}, {confirmation_id} if confirmation_id is not None else set(), now
    )
    return confirmer_id in users or confirmation_id in confirmations


async def _ignore_states(
    session: AsyncSession, rows: list[models.Confirmation], now: datetime
) -> dict[UUID, bool]:
    users, confirmations = await _ignore_reasons(
        session,
        {row.confirmer_id for row in rows if row.confirmer_id is not None},
        {row.id for row in rows},
        now,
    )
    return {
        row.id: row.confirmer_id is not None
        and (row.confirmer_id in users or row.id in confirmations)
        for row in rows
    }


async def _live_confirmations(
    session: AsyncSession,
    fact_type: ConfirmationFactType,
    fact_id: UUID,
    subject_user_id: UUID,
) -> list[models.Confirmation]:
    return list(
        await session.scalars(
            select(models.Confirmation).where(
                models.Confirmation.fact_type == fact_type,
                models.Confirmation.fact_id == fact_id,
                models.Confirmation.subject_user_id == subject_user_id,
                models.Confirmation.withdrawn_at.is_(None),
            )
        )
    )


def _trust(
    fact_type: ConfirmationFactType,
    role: str,
    rows: list[models.Confirmation],
    states: dict[UUID, bool],
) -> Trust:
    peers: set[UUID] = set()
    has_peer = False
    for row in rows:
        if row.ignored or states[row.id]:
            continue
        source = ConfirmationSource(row.source)
        trust = _source_trust(fact_type, role, source)
        if trust == Trust.TRUSTED:
            return Trust.TRUSTED
        if trust == Trust.PEER:
            has_peer = True
            if source == ConfirmationSource.PEER and row.confirmer_id is not None:
                peers.add(row.confirmer_id)
    if fact_type == ConfirmationFactType.PROJECT_MEMBER and role == "lead" and len(peers) >= 2:
        return Trust.TRUSTED
    return Trust.PEER if has_peer else Trust.SELF


async def fact_trust(
    session: AsyncSession,
    *,
    fact_type: ConfirmationFactType,
    fact_id: UUID,
    subject_user_id: UUID,
    now: datetime,
) -> Trust:
    fact = await _resolve_fact(session, fact_type, fact_id, subject_user_id)
    rows = await _live_confirmations(session, fact_type, fact_id, subject_user_id)
    return _trust(fact_type, fact.role, rows, await _ignore_states(session, rows, now))


async def recompute_fact_trust(
    session: AsyncSession,
    jobs: procrastinate.App,
    *,
    fact_type: ConfirmationFactType,
    fact_id: UUID,
    subject_user_id: UUID,
    now: datetime,
) -> Trust:
    fact = await _resolve_fact(session, fact_type, fact_id, subject_user_id, lock=True)
    rows = await _live_confirmations(session, fact_type, fact_id, subject_user_id)
    states = await _ignore_states(session, rows, now)
    for row in rows:
        if row.ignored != states[row.id]:
            row.ignored = states[row.id]
    trust = _trust(fact_type, fact.role, rows, states)
    if getattr(fact.row, fact.trust_column) != trust:
        setattr(fact.row, fact.trust_column, trust)
    await defer_job(session, jobs, JobName.RECOMPUTE_MEMBER, user_id=str(subject_user_id))
    return trust


async def add_confirmation(
    session: AsyncSession,
    jobs: procrastinate.App,
    *,
    fact_type: ConfirmationFactType,
    fact_id: UUID,
    subject_user_id: UUID,
    confirmer_id: UUID | None,
    source: ConfirmationSource,
    now: datetime,
) -> models.Confirmation:
    fact = await _resolve_fact(session, fact_type, fact_id, subject_user_id, lock=True)
    if (
        (isinstance(fact.row, models.EventAttendance) and fact.row.status != "attended")
        or (isinstance(fact.row, models.ProjectMember) and fact.row.state != "confirmed")
        or (isinstance(fact.row, models.Mentorship) and fact.row.status == "cancelled")
    ):
        raise ProblemError(ErrorCode.INVALID_STATE)
    if confirmer_id == subject_user_id:
        raise ProblemError(ErrorCode.CANNOT_CONFIRM_OWN_FACT)
    if _source_trust(fact_type, fact.role, source) is None:
        raise ValueError("Source is not allowed for this fact")
    if confirmer_id is None and source != ConfirmationSource.SYSTEM:
        raise ValueError("Only system confirmations may omit a confirmer")
    existing = await session.scalar(
        select(models.Confirmation).where(
            models.Confirmation.fact_type == fact_type,
            models.Confirmation.fact_id == fact_id,
            models.Confirmation.confirmer_id == confirmer_id,
            models.Confirmation.withdrawn_at.is_(None),
        )
    )
    if existing is not None:
        return existing
    row = models.Confirmation(
        fact_type=fact_type,
        fact_id=fact_id,
        subject_user_id=subject_user_id,
        confirmer_id=confirmer_id,
        source=source,
        ignored=await _ignored(session, confirmer_id, None, now),
        created_at=now,
    )
    session.add(row)
    await session.flush()
    await recompute_fact_trust(
        session,
        jobs,
        fact_type=fact_type,
        fact_id=fact_id,
        subject_user_id=subject_user_id,
        now=now,
    )
    return row


def withdraw_allowed(
    confirmation: models.Confirmation, actor_id: UUID, now: datetime, withdraw_days: int
) -> bool:
    return (
        confirmation.confirmer_id == actor_id
        and confirmation.withdrawn_at is None
        and now <= confirmation.created_at + timedelta(days=withdraw_days)
    )


async def withdraw_confirmation(
    session: AsyncSession,
    jobs: procrastinate.App,
    confirmation_id: UUID,
    actor_id: UUID,
    now: datetime,
) -> None:
    row = await session.scalar(
        select(models.Confirmation)
        .where(models.Confirmation.id == confirmation_id)
        .with_for_update()
    )
    if row is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if row.confirmer_id != actor_id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if row.withdrawn_at is not None:
        return
    settings = await load_settings(session)
    if not withdraw_allowed(row, actor_id, now, settings.confirmation_withdraw_days):
        raise ProblemError(ErrorCode.CONFIRMATION_WITHDRAW_CLOSED)
    row.withdrawn_at = now
    await recompute_fact_trust(
        session,
        jobs,
        fact_type=ConfirmationFactType(row.fact_type),
        fact_id=row.fact_id,
        subject_user_id=row.subject_user_id,
        now=now,
    )


async def _refresh_rows(
    session: AsyncSession, jobs: procrastinate.App, rows: list[models.Confirmation], now: datetime
) -> list[UUID]:
    states = await _ignore_states(session, rows, now)
    affected: set[tuple[ConfirmationFactType, UUID, UUID]] = set()
    for row in rows:
        if row.ignored != states[row.id]:
            row.ignored = states[row.id]
            affected.add((ConfirmationFactType(row.fact_type), row.fact_id, row.subject_user_id))
    for fact_type, fact_id, subject_user_id in sorted(
        affected, key=lambda key: (key[0].value, str(key[1]), str(key[2]))
    ):
        await recompute_fact_trust(
            session,
            jobs,
            fact_type=fact_type,
            fact_id=fact_id,
            subject_user_id=subject_user_id,
            now=now,
        )
    return sorted({key[2] for key in affected}, key=str)


async def refresh_member_confirmations(
    session: AsyncSession, jobs: procrastinate.App, member_id: UUID, now: datetime
) -> list[UUID]:
    rows = list(
        await session.scalars(
            select(models.Confirmation)
            .where(
                models.Confirmation.confirmer_id == member_id,
                models.Confirmation.withdrawn_at.is_(None),
            )
            .order_by(models.Confirmation.id)
            .with_for_update()
        )
    )
    return await _refresh_rows(session, jobs, rows, now)


async def refresh_confirmation(
    session: AsyncSession, jobs: procrastinate.App, confirmation_id: UUID, now: datetime
) -> UUID | None:
    row = await session.scalar(
        select(models.Confirmation)
        .where(models.Confirmation.id == confirmation_id)
        .with_for_update()
    )
    if row is None or row.withdrawn_at is not None:
        return None
    await _refresh_rows(session, jobs, [row], now)
    return row.subject_user_id


async def confirmation_candidates(
    session: AsyncSession, viewer_id: UUID, now: datetime
) -> list[ConfirmationCandidate]:
    own_attendance = aliased(models.EventAttendance)
    attendance_rows = await session.execute(
        select(models.EventAttendance, models.Event, own_attendance.role)
        .join(models.Event, models.Event.id == models.EventAttendance.event_id)
        .join(models.User, models.User.id == models.EventAttendance.user_id)
        .outerjoin(
            own_attendance,
            (own_attendance.event_id == models.Event.id) & (own_attendance.user_id == viewer_id),
        )
        .where(
            models.Event.status.in_(["published", "archived"]),
            models.Event.ends_at <= now,
            models.EventAttendance.status == "attended",
            models.EventAttendance.user_id != viewer_id,
            models.User.banned_at.is_(None),
            or_(
                own_attendance.status == "attended",
                (models.Event.origin == "member") & (models.Event.created_by == viewer_id),
            ),
            ~exists().where(
                models.Confirmation.fact_type == ConfirmationFactType.ATTENDANCE,
                models.Confirmation.fact_id == models.EventAttendance.id,
                models.Confirmation.confirmer_id == viewer_id,
                models.Confirmation.withdrawn_at.is_(None),
            ),
        )
    )
    candidates: list[ConfirmationCandidate] = []
    for attendance, event, own_role in attendance_rows:
        source = ConfirmationSource.PEER
        if event.created_by == viewer_id:
            source = ConfirmationSource.HOST if own_role == "host" else ConfirmationSource.ORGANIZER
        candidates.append(
            ConfirmationCandidate(
                ConfirmationFactType.ATTENDANCE,
                attendance.id,
                attendance.user_id,
                source,
                "event",
                event.id,
                event.title,
            )
        )
    own_membership = aliased(models.ProjectMember)
    project_rows = await session.execute(
        select(models.ProjectMember, models.Project, own_membership.role)
        .join(models.Project, models.Project.id == models.ProjectMember.project_id)
        .join(models.User, models.User.id == models.ProjectMember.user_id)
        .join(
            own_membership,
            (own_membership.project_id == models.Project.id)
            & (own_membership.user_id == viewer_id)
            & (own_membership.state == "confirmed")
            & own_membership.left_at.is_(None),
        )
        .where(
            models.ProjectMember.state == "confirmed",
            models.ProjectMember.user_id != viewer_id,
            models.User.banned_at.is_(None),
            ~exists().where(
                models.Confirmation.fact_type == ConfirmationFactType.PROJECT_MEMBER,
                models.Confirmation.fact_id == models.ProjectMember.id,
                models.Confirmation.confirmer_id == viewer_id,
                models.Confirmation.withdrawn_at.is_(None),
            ),
        )
    )
    for member, project, own_role in project_rows:
        source = (
            ConfirmationSource.LEAD
            if own_role == "lead" and member.role == "member"
            else ConfirmationSource.PEER
        )
        candidates.append(
            ConfirmationCandidate(
                ConfirmationFactType.PROJECT_MEMBER,
                member.id,
                member.user_id,
                source,
                "project",
                project.id,
                project.name,
            )
        )
    return sorted(
        candidates,
        key=lambda candidate: (
            candidate.subject_type,
            candidate.title,
            str(candidate.subject_id),
            candidate.fact_type.value,
            str(candidate.fact_id),
        ),
    )


async def confirmation_target(
    session: AsyncSession,
    viewer_id: UUID,
    fact_type: ConfirmationFactType,
    fact_id: UUID,
    now: datetime,
) -> ConfirmationCandidate:
    if fact_type == ConfirmationFactType.ATTENDANCE:
        attendance = await session.get(models.EventAttendance, fact_id)
        if attendance is None:
            raise ProblemError(ErrorCode.NOT_FOUND)
        subject_user_id = attendance.user_id
        event = await session.get(models.Event, attendance.event_id)
        if event is None:
            raise ProblemError(ErrorCode.NOT_FOUND)
        subject_type, subject_id, title = "event", event.id, event.title
    elif fact_type == ConfirmationFactType.PROJECT_MEMBER:
        member = await session.get(models.ProjectMember, fact_id)
        if member is None:
            raise ProblemError(ErrorCode.NOT_FOUND)
        subject_user_id = member.user_id
        project = await session.get(models.Project, member.project_id)
        if project is None:
            raise ProblemError(ErrorCode.NOT_FOUND)
        subject_type, subject_id, title = "project", project.id, project.name
    elif fact_type == ConfirmationFactType.MENTORSHIP_SIDE:
        if await session.get(models.Mentorship, fact_id) is None:
            raise ProblemError(ErrorCode.NOT_FOUND)
        raise ProblemError(ErrorCode.FORBIDDEN)
    else:
        raise ValueError("Unknown confirmation fact type")
    if subject_user_id == viewer_id:
        raise ProblemError(ErrorCode.CANNOT_CONFIRM_OWN_FACT)
    existing = await session.scalar(
        select(models.Confirmation).where(
            models.Confirmation.fact_type == fact_type,
            models.Confirmation.fact_id == fact_id,
            models.Confirmation.confirmer_id == viewer_id,
            models.Confirmation.withdrawn_at.is_(None),
        )
    )
    if existing is not None:
        return ConfirmationCandidate(
            fact_type,
            fact_id,
            subject_user_id,
            ConfirmationSource(existing.source),
            subject_type,
            subject_id,
            title,
        )
    for candidate in await confirmation_candidates(session, viewer_id, now):
        if candidate.fact_type == fact_type and candidate.fact_id == fact_id:
            return candidate
    raise ProblemError(ErrorCode.FORBIDDEN)


def confirmation_view(
    confirmation: models.Confirmation, viewer_id: UUID, now: datetime, withdraw_days: int
) -> schemas.Confirmation:
    return schemas.Confirmation(
        id=confirmation.id,
        fact_type=ConfirmationFactType(confirmation.fact_type),
        fact_id=confirmation.fact_id,
        subject_user_id=confirmation.subject_user_id,
        confirmer_id=confirmation.confirmer_id,
        source=ConfirmationSource(confirmation.source),
        ignored=confirmation.ignored,
        created_at=confirmation.created_at,
        withdrawn_at=confirmation.withdrawn_at,
        allowed_actions=[schemas.ConfirmationAction.WITHDRAW]
        if withdraw_allowed(confirmation, viewer_id, now, withdraw_days)
        else [],
    )
