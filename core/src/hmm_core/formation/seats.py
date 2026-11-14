from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast
from uuid import UUID

import procrastinate
from hmm_contracts.algo import Rec
from hmm_contracts.common import (
    ActorKind,
    AuditAction,
    AuditEntityType,
    ConfirmKind,
    EnrollmentStatus,
    ErrorCode,
    EventStatus,
    FormationPhase,
    JoinedVia,
    LeftReason,
    MemberRole,
    MemberState,
    MergeProposalStatus,
    ProjectStatus,
    SwapRequestStatus,
    TeamStatus,
    Trust,
)
from hmm_contracts.notifications import (
    AlertKind,
    NotificationCandidate,
    NotificationPerson,
    ReserveOfferPayload,
    SeatAutofilledPayload,
    SeatOpenedPayload,
    TeamDisbandedPayload,
    TeamProposedPayload,
)
from pydantic import JsonValue
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.formation import schemas
from hmm_core.formation.common import (
    departed_team_user_ids,
    dnd_active,
    enqueue_participation_jobs,
    formation_row,
    formation_sizes,
    formation_timing,
    invite_project,
    live_enrollment_row,
    note_lead_action,
)
from hmm_core.formation.enrollments import live_membership, resolve_preferred_area
from hmm_core.formation.lead_offers import close_pending_lead_offers
from hmm_core.formation.leads import _locked_team, close_project_pending_items
from hmm_core.notifications.service import notify, raise_alert, resolve_alerts
from hmm_core.platform.audit import record_audit
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.settings import load_settings


@dataclass(frozen=True, slots=True)
class ReserveCandidate:
    enrollment: models.Enrollment
    user: models.User
    area_key: str | None
    reason: str
    rank: int


async def reserve_candidates(
    session: AsyncSession, project: models.Project, event: models.Event, now: datetime
) -> list[ReserveCandidate]:
    from hmm_core.recommendations import service as recommendations

    if project.event_id is None:
        return []
    by_user = {
        user.id: (enrollment, user)
        for enrollment, user in await session.execute(
            select(models.Enrollment, models.User)
            .join(models.User, models.User.id == models.Enrollment.user_id)
            .where(
                models.Enrollment.event_id == event.id,
                models.Enrollment.status == EnrollmentStatus.RESERVE.value,
                models.User.banned_at.is_(None),
            )
            .order_by(models.Enrollment.created_at, models.Enrollment.id)
        )
    }
    pending = set(
        await session.scalars(
            select(models.ProjectMember.user_id).where(
                models.ProjectMember.event_id == event.id,
                models.ProjectMember.left_at.is_(None),
                models.ProjectMember.is_team_row.is_(True),
                models.ProjectMember.state == MemberState.PROPOSED.value,
                models.ProjectMember.joined_via == JoinedVia.RESERVE_OFFER.value,
            )
        )
    )
    departed = await departed_team_user_ids(session, project.id, list(by_user))
    by_user = {
        user_id: (enrollment, user)
        for user_id, (enrollment, user) in by_user.items()
        if user_id not in pending and user_id not in departed and not dnd_active(user, now)
    }
    ordered: list[ReserveCandidate] = []
    placed: set[UUID] = set()
    row = await recommendations.latest_ready_row(session, project.id)
    if row is not None:
        for item in cast(list[JsonValue], row.enrolled or []):
            rec = Rec.model_validate(item)
            if rec.user_id not in by_user or rec.user_id in placed:
                continue
            enrollment, user = by_user[rec.user_id]
            ordered.append(
                ReserveCandidate(
                    enrollment, user, rec.suggested_area, rec.reasons[0], len(ordered) + 1
                )
            )
            placed.add(rec.user_id)
    remaining = sorted(
        (pair for user_id, pair in by_user.items() if user_id not in placed),
        key=lambda pair: (
            pair[0].reserve_rank is None,
            pair[0].reserve_rank or 0,
            pair[0].created_at,
            str(pair[0].id),
        ),
    )
    area_ids = {enrollment.preferred_area_id for enrollment, _ in remaining} - {None}
    areas = {
        area_id: key
        for area_id, key in await session.execute(
            select(models.Area.id, models.Area.key).where(models.Area.id.in_(area_ids))
        )
    }
    for enrollment, user in remaining:
        ordered.append(
            ReserveCandidate(
                enrollment,
                user,
                areas.get(enrollment.preferred_area_id),
                "Recommended from the reserve",
                len(ordered) + 1,
            )
        )
    return ordered


async def seat_quick_pick(
    session: AsyncSession,
    project: models.Project,
    event: models.Event,
    now: datetime,
    *,
    limit: int = 3,
) -> tuple[NotificationCandidate, ...]:
    return tuple(
        NotificationCandidate(
            user_id=candidate.user.id,
            display_name=candidate.user.display_name,
            rank=candidate.rank,
            area_key=candidate.area_key,
            reason=candidate.reason,
        )
        for candidate in (await reserve_candidates(session, project, event, now))[:limit]
    )


async def open_seat(
    session: AsyncSession, project: models.Project, event: models.Event, now: datetime
) -> None:
    if (
        project.team_status != TeamStatus.LOCKED.value
        or event.status == EventStatus.CANCELLED.value
    ):
        return None
    formation = await session.get(models.EventFormation, event.id)
    if formation is None:
        return None
    settings = await load_settings(session)
    sizes = formation_sizes(event, formation, settings)
    timing = formation_timing(event, settings, now)
    member_count = await _member_count(session, project.id)
    reserve_count = (
        await session.scalar(
            select(func.count())
            .select_from(models.Enrollment)
            .join(models.User, models.User.id == models.Enrollment.user_id)
            .where(
                models.Enrollment.event_id == event.id,
                models.Enrollment.status == EnrollmentStatus.RESERVE.value,
                models.User.banned_at.is_(None),
            )
        )
        or 0
    )
    if member_count >= sizes.min_size:
        await resolve_alerts(
            session,
            f"team_below_min:{project.id}",
            now,
            note="The team reached the minimum size",
        )
    elif reserve_count == 0 or now >= timing.reg_deadline:
        await raise_alert(
            session,
            AlertKind.TEAM_BELOW_MIN,
            now,
            title=f"Team below minimum size: {project.name}",
            message=f"{member_count} of {sizes.min_size} members with an empty reserve",
            link=f"/admin/projects/{project.id}",
            subject_type="project",
            subject_id=project.id,
            subject_title=project.name,
            dedupe_key=f"team_below_min:{project.id}",
        )
    if member_count >= sizes.target:
        project.seat_open_since = None
        await session.flush()
        return None
    seat_open_since = project.seat_open_since
    if seat_open_since is None:
        seat_open_since = now
        project.seat_open_since = now
        await session.flush()
    if project.seats_closed_at is not None:
        if member_count >= sizes.min_size:
            return None
        project.seats_closed_at = None
        await session.flush()
    if now >= timing.reg_deadline or project.lead_user_id is None:
        return None
    head_start_ends_at = min(
        seat_open_since + timedelta(hours=settings.seat_fill_hours), timing.reg_deadline
    )
    quick_pick = await seat_quick_pick(session, project, event, now)
    await notify(
        session,
        project.lead_user_id,
        SeatOpenedPayload(
            link=f"/projects/{project.id}",
            event_id=event.id,
            event_title=event.title,
            project_id=project.id,
            team_name=project.name,
            open_seats=sizes.target - member_count,
            head_start_ends_at=head_start_ends_at,
            reg_deadline=timing.reg_deadline,
            quick_pick=quick_pick,
        ),
        now,
        dedupe_key=f"seat_opened:{project.id}:{seat_open_since.isoformat()}",
    )
    return None


async def _member_count(session: AsyncSession, project_id: UUID) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(models.ProjectMember)
            .where(
                models.ProjectMember.project_id == project_id,
                models.ProjectMember.left_at.is_(None),
                models.ProjectMember.is_team_row.is_(True),
            )
        )
        or 0
    )


async def close_seats(
    session: AsyncSession, project_id: UUID, actor: models.User, now: datetime
) -> models.Project:
    event, project = await _locked_team(session, project_id, actor)
    formation = await formation_row(session, event.id)
    if FormationPhase(formation.phase) == FormationPhase.FORMING:
        raise ProblemError(ErrorCode.FORMATION_IN_PROGRESS, "Formation is running, retry shortly")
    if project.team_status != TeamStatus.LOCKED.value:
        raise ProblemError(
            ErrorCode.INVALID_STATE, "Seats can only be opened or closed on a locked team"
        )
    if project.lead_user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    await note_lead_action(session, project, actor, now)
    if project.seats_closed_at is not None:
        await session.refresh(project)
        return project
    settings = await load_settings(session)
    sizes = formation_sizes(event, formation, settings)
    member_count = await _member_count(session, project.id)
    if member_count < sizes.min_size:
        raise ProblemError(
            ErrorCode.SEATS_CLOSE_BELOW_MIN,
            f"The team has {member_count} of {sizes.min_size} members",
        )
    project.seats_closed_at = now
    await session.flush()
    await session.refresh(project)
    return project


async def reopen_seats(
    session: AsyncSession, project_id: UUID, actor: models.User, now: datetime
) -> models.Project:
    event, project = await _locked_team(session, project_id, actor)
    formation = await formation_row(session, event.id)
    if FormationPhase(formation.phase) == FormationPhase.FORMING:
        raise ProblemError(ErrorCode.FORMATION_IN_PROGRESS, "Formation is running, retry shortly")
    if project.team_status != TeamStatus.LOCKED.value:
        raise ProblemError(
            ErrorCode.INVALID_STATE, "Seats can only be opened or closed on a locked team"
        )
    if project.lead_user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    await note_lead_action(session, project, actor, now)
    if project.seats_closed_at is None:
        await session.refresh(project)
        return project
    project.seats_closed_at = None
    await session.flush()
    await open_seat(session, project, event, now)
    await session.refresh(project)
    return project


async def add_reserve_member(
    session: AsyncSession,
    jobs: procrastinate.App,
    project_id: UUID,
    body: schemas.AddMemberBody,
    actor: models.User,
    now: datetime,
) -> tuple[models.Project, models.ProjectMember]:
    project, event = await invite_project(session, project_id, actor, lock=True)
    if event is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if project.lead_user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    formation = await formation_row(session, event.id)
    if FormationPhase(formation.phase) == FormationPhase.FORMING:
        raise ProblemError(ErrorCode.FORMATION_IN_PROGRESS, "Formation is running, retry shortly")
    settings = await load_settings(session)
    timing = formation_timing(event, settings, now)
    sizes = formation_sizes(event, formation, settings)
    if project.team_status not in (TeamStatus.REVIEW.value, TeamStatus.LOCKED.value):
        raise ProblemError(
            ErrorCode.INVALID_STATE,
            "Members can only be added from the reserve in review or while locked",
        )
    if project.team_status == TeamStatus.LOCKED.value and now >= timing.reg_deadline:
        raise ProblemError(
            ErrorCode.ENROLLMENT_CLOSED, "Seat filling stops at the registration deadline"
        )
    user = await session.get(models.User, body.user_id)
    if user is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if user.banned_at is not None:
        raise ProblemError(ErrorCode.INVALID_STATE, "This member is banned")
    if body.user_id in await departed_team_user_ids(session, project.id, [body.user_id]):
        raise ProblemError(ErrorCode.INVALID_STATE, "This member already left this team")
    if await live_membership(session, event.id, body.user_id) is not None:
        raise ProblemError(
            ErrorCode.INVALID_STATE, "The member is already on a team for this event"
        )
    enrollment = await live_enrollment_row(session, event.id, body.user_id, for_update=True)
    if enrollment is None or enrollment.status != EnrollmentStatus.RESERVE.value:
        raise ProblemError(ErrorCode.NOT_ENROLLED, "The member is not on the reserve")
    live_count = (
        await session.scalar(
            select(func.count())
            .select_from(models.ProjectMember)
            .where(
                models.ProjectMember.project_id == project.id,
                models.ProjectMember.left_at.is_(None),
            )
        )
        or 0
    )
    if live_count >= sizes.max_size:
        raise ProblemError(ErrorCode.TEAM_FULL, f"Team has reached max size {sizes.max_size}")
    await note_lead_action(session, project, actor, now)
    area_id = await resolve_preferred_area(session, body.area_key)
    review_ends = project.review_ends_at or formation.review_ends_at or now
    confirm_by = min(
        max(review_ends, now + timedelta(hours=settings.reserve_offer_hours)), timing.reg_deadline
    )
    member = models.ProjectMember(
        project_id=project.id,
        event_id=event.id,
        user_id=user.id,
        role=MemberRole.MEMBER.value,
        area_id=area_id,
        state=MemberState.PROPOSED.value,
        trust=Trust.SELF.value,
        joined_via=JoinedVia.RESERVE_ADD.value,
        proposal_reason="Added from the reserve by the lead",
        confirm_by=confirm_by,
        confirm_kind=ConfirmKind.PLACEMENT.value,
        is_team_row=True,
        joined_at=now,
        confirmed_at=None,
    )
    session.add(member)
    await session.flush()
    enrollment.status = EnrollmentStatus.PLACED.value
    enrollment.status_at = now
    enrollment.reserve_rank = None
    await session.flush()
    await open_seat(session, project, event, now)
    rows = (
        await session.execute(
            select(models.ProjectMember, models.User)
            .join(models.User, models.User.id == models.ProjectMember.user_id)
            .where(
                models.ProjectMember.project_id == project.id,
                models.ProjectMember.left_at.is_(None),
                models.ProjectMember.is_team_row.is_(True),
            )
            .order_by(
                models.ProjectMember.role != MemberRole.LEAD,
                models.ProjectMember.joined_at,
                models.ProjectMember.id,
            )
        )
    ).all()
    people = tuple(
        NotificationPerson(user_id=row_user.id, display_name=row_user.display_name)
        for _, row_user in rows
    )
    await notify(
        session,
        user.id,
        TeamProposedPayload(
            link=f"/projects/{project.id}",
            event_id=event.id,
            event_title=event.title,
            project_id=project.id,
            team_name=project.name,
            project_member_id=member.id,
            confirm_by=confirm_by,
            timezone=event.timezone,
            members=people,
            proposal_reason=cast(str, member.proposal_reason),
            area_key=body.area_key,
        ),
        now,
        dedupe_key=f"team_proposed:{member.id}",
    )
    await enqueue_participation_jobs(session, jobs, event.id, reserve=True)
    await session.flush()
    await session.refresh(project)
    await session.refresh(member)
    return project, member


async def autofill_seats(
    session: AsyncSession, jobs: procrastinate.App, event: models.Event, now: datetime
) -> int:
    if event.status != EventStatus.PUBLISHED.value:
        return 0
    formation = await session.get(models.EventFormation, event.id)
    if formation is None or FormationPhase(formation.phase) != FormationPhase.LOCKED:
        return 0
    settings = await load_settings(session)
    timing = formation_timing(event, settings, now)
    if now >= timing.reg_deadline:
        return 0
    sizes = formation_sizes(event, formation, settings)
    projects = list(
        await session.scalars(
            select(models.Project)
            .where(
                models.Project.event_id == event.id,
                models.Project.team_status == TeamStatus.LOCKED.value,
            )
            .order_by(models.Project.created_at, models.Project.id)
        )
    )
    if not projects:
        return 0
    counts = {
        project_id: count
        for project_id, count in await session.execute(
            select(models.ProjectMember.project_id, func.count())
            .where(
                models.ProjectMember.event_id == event.id,
                models.ProjectMember.left_at.is_(None),
                models.ProjectMember.is_team_row.is_(True),
            )
            .group_by(models.ProjectMember.project_id)
        )
    }
    for project in projects:
        if (
            project.seats_closed_at is None
            and counts.get(project.id, 0) < sizes.target
            and project.seat_open_since is None
        ):
            await open_seat(session, project, event, now)
    due = [
        project
        for project in projects
        if project.seats_closed_at is None
        and counts.get(project.id, 0) < sizes.target
        and project.seat_open_since is not None
        and now >= project.seat_open_since + timedelta(hours=settings.seat_fill_hours)
    ]
    due.sort(
        key=lambda project: (
            0
            if counts.get(project.id, 0) < sizes.min_size
            else 1
            if counts.get(project.id, 0) < sizes.viable
            else 2,
            cast(datetime, project.seat_open_since),
            project.created_at,
            str(project.id),
        )
    )
    created = 0
    used: set[UUID] = set()
    for project in due:
        candidates = await reserve_candidates(session, project, event, now)
        for candidate in candidates:
            if candidate.user.id in used:
                continue
            counts[project.id] = await _member_count(session, project.id)
            wanted = sizes.target - counts[project.id]
            if wanted <= 0:
                break
            confirm_by = min(
                now + timedelta(hours=settings.reserve_offer_hours), timing.reg_deadline
            )
            member = models.ProjectMember(
                project_id=project.id,
                event_id=event.id,
                user_id=candidate.user.id,
                role=MemberRole.MEMBER.value,
                area_id=None,
                state=MemberState.PROPOSED.value,
                trust=Trust.SELF.value,
                joined_via=JoinedVia.RESERVE_OFFER.value,
                proposal_reason="Offered an open seat from the reserve",
                confirm_by=confirm_by,
                confirm_kind=ConfirmKind.PLACEMENT.value,
                is_team_row=True,
                joined_at=now,
                confirmed_at=None,
            )
            session.add(member)
            await session.flush()
            candidate.enrollment.status = EnrollmentStatus.PLACED.value
            candidate.enrollment.status_at = now
            candidate.enrollment.reserve_rank = None
            await session.flush()
            await record_audit(
                session,
                actor=None,
                actor_kind=ActorKind.SYSTEM,
                action=AuditAction.SYSTEM_TRANSITION,
                entity_type=AuditEntityType.PROJECT_MEMBER,
                entity_id=member.id,
                before=None,
                after={
                    "user_id": str(candidate.user.id),
                    "state": MemberState.PROPOSED.value,
                    "joined_via": JoinedVia.RESERVE_OFFER.value,
                    "confirm_by": confirm_by.isoformat(),
                },
                reason="Offered an open seat from the reserve",
            )
            rows = (
                await session.execute(
                    select(models.ProjectMember, models.User)
                    .join(models.User, models.User.id == models.ProjectMember.user_id)
                    .where(
                        models.ProjectMember.project_id == project.id,
                        models.ProjectMember.left_at.is_(None),
                        models.ProjectMember.is_team_row.is_(True),
                    )
                    .order_by(
                        models.ProjectMember.role != MemberRole.LEAD,
                        models.ProjectMember.joined_at,
                        models.ProjectMember.id,
                    )
                )
            ).all()
            people = tuple(
                NotificationPerson(user_id=user.id, display_name=user.display_name)
                for _, user in rows
            )
            await notify(
                session,
                candidate.user.id,
                ReserveOfferPayload(
                    link=f"/projects/{project.id}",
                    event_id=event.id,
                    event_title=event.title,
                    project_id=project.id,
                    team_name=project.name,
                    project_member_id=member.id,
                    confirm_by=confirm_by,
                    timezone=event.timezone,
                    members=people,
                    rank=candidate.rank,
                    proposal_reason=cast(str, member.proposal_reason),
                ),
                now,
                dedupe_key=f"reserve_offer:{member.id}",
            )
            used.add(candidate.user.id)
            wanted -= 1
            counts[project.id] += 1
            created += 1
        await open_seat(session, project, event, now)
    if created:
        await enqueue_participation_jobs(session, jobs, event.id, reserve=True)
    await session.flush()
    return created


async def notify_seat_autofilled(
    session: AsyncSession,
    project: models.Project,
    event: models.Event,
    member: models.ProjectMember,
    now: datetime,
) -> None:
    if project.lead_user_id is None or project.lead_user_id == member.user_id:
        return None
    formation = await session.get(models.EventFormation, event.id)
    if formation is None:
        return None
    settings = await load_settings(session)
    sizes = formation_sizes(event, formation, settings)
    user = await session.get(models.User, member.user_id)
    if user is None:
        return None
    member_count = await _member_count(session, project.id)
    await notify(
        session,
        project.lead_user_id,
        SeatAutofilledPayload(
            link=f"/projects/{project.id}",
            event_id=event.id,
            event_title=event.title,
            project_id=project.id,
            team_name=project.name,
            member=NotificationPerson(user_id=user.id, display_name=user.display_name),
            open_seats=max(0, sizes.target - member_count),
        ),
        now,
        dedupe_key=f"seat_autofilled:{member.id}",
    )
    return None


async def registration_cutoff(
    session: AsyncSession, jobs: procrastinate.App, event: models.Event, now: datetime
) -> int:
    if event.status != EventStatus.PUBLISHED.value:
        return 0
    formation = await session.get(models.EventFormation, event.id)
    if formation is None or FormationPhase(formation.phase) != FormationPhase.LOCKED:
        return 0
    if formation.reg_cutoff_at is not None:
        return 0
    settings = await load_settings(session)
    timing = formation_timing(event, settings, now)
    if now < timing.reg_deadline:
        return 0
    sizes = formation_sizes(event, formation, settings)
    rows = (
        await session.execute(
            select(models.Project.id, models.Project.created_at)
            .where(
                models.Project.event_id == event.id,
                models.Project.team_status == TeamStatus.LOCKED.value,
            )
            .order_by(models.Project.created_at, models.Project.id)
        )
    ).all()
    counts = {
        project_id: count
        for project_id, count in await session.execute(
            select(models.ProjectMember.project_id, func.count())
            .where(
                models.ProjectMember.event_id == event.id,
                models.ProjectMember.left_at.is_(None),
                models.ProjectMember.is_team_row.is_(True),
            )
            .group_by(models.ProjectMember.project_id)
        )
    }
    disbanded = 0
    for project_id, _ in rows:
        if counts.get(project_id, 0) >= sizes.min_size:
            continue
        pending_offer = await session.scalar(
            select(models.ProjectMember.id)
            .where(
                models.ProjectMember.project_id == project_id,
                models.ProjectMember.left_at.is_(None),
                models.ProjectMember.is_team_row.is_(True),
                models.ProjectMember.state == MemberState.PROPOSED.value,
                models.ProjectMember.joined_via == JoinedVia.RESERVE_OFFER.value,
            )
            .limit(1)
        )
        if pending_offer is not None:
            continue
        project = await session.scalar(
            select(models.Project)
            .where(models.Project.id == project_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if project is None:
            continue
        previous_lead = project.lead_user_id
        members = list(
            await session.scalars(
                select(models.ProjectMember)
                .where(
                    models.ProjectMember.project_id == project.id,
                    models.ProjectMember.left_at.is_(None),
                    models.ProjectMember.is_team_row.is_(True),
                )
                .order_by(models.ProjectMember.joined_at, models.ProjectMember.id)
            )
        )
        for member in members:
            member.left_at = now
            member.left_reason = LeftReason.DISBANDED.value
            row = await live_enrollment_row(session, event.id, member.user_id, for_update=True)
            if row is not None:
                row.status = EnrollmentStatus.RESERVE.value
                row.status_at = now
                row.reserve_rank = None
            await notify(
                session,
                member.user_id,
                TeamDisbandedPayload(
                    link=f"/projects/{project.id}",
                    event_id=event.id,
                    event_title=event.title,
                    project_id=project.id,
                    team_name=project.name,
                    reason="The team was below the minimum size at the registration deadline",
                ),
                now,
                dedupe_key=f"team_disbanded:{project.id}:{member.user_id}",
            )
        project.team_status = TeamStatus.DISBANDED.value
        project.status = ProjectStatus.DISBANDED.value
        project.lead_user_id = None
        project.ready_at = None
        project.seat_open_since = None
        project.last_lead_action_at = now
        await close_pending_lead_offers(session, project.id, now)
        await close_project_pending_items(session, project.id, now, reason="team_disbanded")
        await session.execute(
            update(models.SwapRequest)
            .where(
                models.SwapRequest.status == SwapRequestStatus.PENDING.value,
                or_(
                    models.SwapRequest.from_team_id == project.id,
                    models.SwapRequest.to_team_id == project.id,
                ),
            )
            .values(status=SwapRequestStatus.CLOSED.value)
        )
        await session.execute(
            update(models.MergeProposal)
            .where(
                models.MergeProposal.status == MergeProposalStatus.PENDING.value,
                or_(
                    models.MergeProposal.absorbed_team_id == project.id,
                    models.MergeProposal.target_team_id == project.id,
                ),
            )
            .values(status=MergeProposalStatus.CLOSED.value)
        )
        await record_audit(
            session,
            actor=None,
            actor_kind=ActorKind.SYSTEM,
            action=AuditAction.PROJECT_DISBAND,
            entity_type=AuditEntityType.PROJECT,
            entity_id=project.id,
            before={
                "team_status": TeamStatus.LOCKED.value,
                "lead_user_id": str(previous_lead) if previous_lead is not None else None,
            },
            after={
                "team_status": TeamStatus.DISBANDED.value,
                "reason": "below_min_at_reg_deadline",
            },
            reason="The team was below the minimum size at the registration deadline",
        )
        await raise_alert(
            session,
            AlertKind.TEAM_BELOW_MIN,
            now,
            title=f"Team below minimum size: {project.name}",
            message=(
                f"{counts.get(project_id, 0)} of {sizes.min_size} members "
                "at the registration deadline"
            ),
            link=f"/admin/projects/{project.id}",
            subject_type="project",
            subject_id=project.id,
            subject_title=project.name,
            dedupe_key=f"team_below_min:{project.id}",
        )
        await session.flush()
        disbanded += 1
    formation.reg_cutoff_at = now
    if disbanded:
        await enqueue_participation_jobs(session, jobs, event.id, reserve=True)
    await session.flush()
    return disbanded
