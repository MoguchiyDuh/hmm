from collections.abc import Sequence
from datetime import datetime
from math import ceil
from uuid import UUID

import procrastinate
from hmm_contracts.common import (
    ConfirmKind,
    EnrollmentSource,
    EnrollmentStatus,
    ErrorCode,
    EventKind,
    EventStatus,
    FormationPhase,
    JoinedVia,
    LeftReason,
    MemberRole,
    MemberState,
    TeamStatus,
    Trust,
)
from hmm_contracts.formation import placement_bounds
from sqlalchemy import exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.attendance import service as attendance
from hmm_core.formation import schemas
from hmm_core.formation.common import (
    LIVE_ENROLLMENT_STATES,
    dnd_active,
    effective_score,
    enqueue_participation_jobs,
    formation_event,
    formation_timing,
    latest_terminal_enrollment,
    lead_eligibility,
    live_enrollment_row,
    participation_event,
    projected_team_sizes,
    score_reason,
)
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.pagination import CursorParams, keyset_page
from hmm_core.platform.schemas import AreaScoreSummary, EventRef, ProjectRef, UserRef
from hmm_core.platform.settings import RuntimeSettings, load_settings
from hmm_core.projects import schemas as project_schemas


def enrollment_actions(
    enrollment: models.Enrollment,
    *,
    event: models.Event,
    actor: models.User,
    on_team: bool,
    now: datetime,
) -> list[schemas.EnrollmentAction]:
    own_live = enrollment.user_id == actor.id and enrollment.status in LIVE_ENROLLMENT_STATES
    allowed: set[schemas.EnrollmentAction] = set()
    if own_live and now < event.starts_at:
        allowed.add(schemas.EnrollmentAction.EDIT)
        if not on_team:
            allowed.add(schemas.EnrollmentAction.WITHDRAW)
    return [action for action in schemas.EnrollmentAction if action in allowed]


def formation_actions(
    event: models.Event,
    formation: models.EventFormation,
    *,
    actor: models.User,
    enrollment: models.Enrollment | None,
    on_team: bool,
    lead_fit: models.LeadFit | None,
    now: datetime,
    reg_deadline: datetime,
    settings: RuntimeSettings,
) -> list[schemas.FormationAction]:
    phase = FormationPhase(formation.phase)
    published = event.status == EventStatus.PUBLISHED
    live = enrollment is not None and enrollment.status in LIVE_ENROLLMENT_STATES
    before_start = now < event.starts_at
    allowed: set[schemas.FormationAction] = set()
    if (
        published
        and phase not in (FormationPhase.FORMING, FormationPhase.CLOSED)
        and now < reg_deadline
        and before_start
        and not on_team
        and not live
    ):
        allowed.add(schemas.FormationAction.ENROLL)
    if live and before_start:
        allowed.add(schemas.FormationAction.EDIT_ENROLLMENT)
        if not on_team:
            allowed.add(schemas.FormationAction.WITHDRAW)
    if (
        published
        and phase == FormationPhase.OPEN
        and not on_team
        and not dnd_active(actor, now)
        and lead_fit is not None
        and float(lead_fit.fit) >= settings.lead_fit_threshold
    ):
        allowed.add(schemas.FormationAction.CLAIM_LEAD)
    if actor.is_admin and phase == FormationPhase.OPEN:
        allowed.add(schemas.FormationAction.EDIT)
    if actor.is_admin and phase in (FormationPhase.OPEN, FormationPhase.REVIEW):
        allowed.add(schemas.FormationAction.RUN)
    return [action for action in schemas.FormationAction if action in allowed]


async def live_membership(
    session: AsyncSession, event_id: UUID, user_id: UUID
) -> tuple[models.ProjectMember, models.Project] | None:
    row = (
        await session.execute(
            select(models.ProjectMember, models.Project)
            .join(models.Project, models.Project.id == models.ProjectMember.project_id)
            .where(
                models.ProjectMember.event_id == event_id,
                models.ProjectMember.user_id == user_id,
                models.ProjectMember.left_at.is_(None),
                models.ProjectMember.is_team_row,
            )
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    return row[0], row[1]


async def membership_view(
    session: AsyncSession, member: models.ProjectMember, actor: models.User, now: datetime
) -> project_schemas.ProjectMember:
    area_key = (
        None
        if member.area_id is None
        else await session.scalar(select(models.Area.key).where(models.Area.id == member.area_id))
    )
    user = await session.get_one(models.User, member.user_id)
    actions = (
        [
            project_schemas.ProjectMemberAction.CONFIRM_PLACEMENT,
            project_schemas.ProjectMemberAction.DECLINE_PLACEMENT,
        ]
        if member.user_id == actor.id and member.state == MemberState.PROPOSED
        else []
    )
    return project_schemas.ProjectMember(
        id=member.id,
        project_id=member.project_id,
        event_id=member.event_id,
        user_id=member.user_id,
        user=UserRef(id=user.id, display_name=user.display_name, avatar_url=user.avatar_url),
        role=MemberRole(member.role),
        area_key=area_key,
        state=MemberState(member.state),
        trust=Trust(member.trust),
        joined_via=JoinedVia(member.joined_via),
        proposal_reason=member.proposal_reason,
        confirm_by=member.confirm_by,
        confirm_kind=ConfirmKind(member.confirm_kind) if member.confirm_kind else None,
        is_team_row=member.is_team_row,
        joined_at=member.joined_at,
        confirmed_at=member.confirmed_at,
        left_at=member.left_at,
        left_reason=LeftReason(member.left_reason) if member.left_reason else None,
        allowed_actions=actions,
    )


async def enrollment_views(
    session: AsyncSession,
    enrollments: Sequence[models.Enrollment],
    actor: models.User,
    now: datetime,
) -> list[schemas.Enrollment]:
    if not enrollments:
        return []
    user_ids = {enrollment.user_id for enrollment in enrollments}
    event_ids = {enrollment.event_id for enrollment in enrollments}
    area_ids = {
        enrollment.preferred_area_id
        for enrollment in enrollments
        if enrollment.preferred_area_id is not None
    }
    users = {
        user.id: user
        for user in await session.scalars(select(models.User).where(models.User.id.in_(user_ids)))
    }
    events = {
        event.id: event
        for event in await session.scalars(
            select(models.Event).where(models.Event.id.in_(event_ids))
        )
    }
    areas = (
        {
            area.id: area
            for area in await session.scalars(
                select(models.Area).where(models.Area.id.in_(area_ids))
            )
        }
        if area_ids
        else {}
    )
    teams = {
        (event_id, user_id): project
        for event_id, user_id, project in await session.execute(
            select(models.ProjectMember.event_id, models.ProjectMember.user_id, models.Project)
            .join(models.Project, models.Project.id == models.ProjectMember.project_id)
            .where(
                models.ProjectMember.event_id.in_(event_ids),
                models.ProjectMember.user_id.in_(user_ids),
                models.ProjectMember.left_at.is_(None),
                models.ProjectMember.is_team_row,
            )
        )
    }
    scores: dict[UUID, list[AreaScoreSummary]] = {}
    for score, area in await session.execute(
        select(models.AreaScore, models.Area)
        .join(models.Area, models.Area.id == models.AreaScore.area_id)
        .where(models.AreaScore.user_id.in_(user_ids), models.Area.archived_at.is_(None))
        .order_by(models.Area.sort, models.Area.key)
    ):
        scores.setdefault(score.user_id, []).append(
            AreaScoreSummary(
                area_key=area.key,
                effective=float(effective_score(score)),
                reason=score_reason(score),
            )
        )
    top_scores = {
        user_id: sorted(rows, key=lambda row: (-row.effective, row.area_key))[:3]
        for user_id, rows in scores.items()
    }
    views: list[schemas.Enrollment] = []
    for enrollment in enrollments:
        event = events[enrollment.event_id]
        user = users[enrollment.user_id]
        project = teams.get((enrollment.event_id, enrollment.user_id))
        views.append(
            schemas.Enrollment(
                id=enrollment.id,
                event_id=enrollment.event_id,
                event=EventRef(
                    id=event.id,
                    title=event.title,
                    kind=EventKind(event.kind),
                    starts_at=event.starts_at,
                    ends_at=event.ends_at,
                    timezone=event.timezone,
                ),
                user=UserRef(
                    id=user.id, display_name=user.display_name, avatar_url=user.avatar_url
                ),
                preferred_area_key=(
                    areas[enrollment.preferred_area_id].key
                    if enrollment.preferred_area_id is not None
                    and enrollment.preferred_area_id in areas
                    else None
                ),
                note=enrollment.note,
                willing_to_lead=enrollment.willing_to_lead,
                status=EnrollmentStatus(enrollment.status),
                source=EnrollmentSource(enrollment.source),
                reserve_rank=enrollment.reserve_rank,
                status_at=enrollment.status_at,
                created_at=enrollment.created_at,
                updated_at=enrollment.updated_at,
                top_scores=top_scores.get(enrollment.user_id, []),
                team=(
                    ProjectRef(
                        id=project.id,
                        name=project.name,
                        event_id=project.event_id,
                        team_status=TeamStatus(project.team_status)
                        if project.team_status
                        else None,
                    )
                    if project
                    else None
                ),
                allowed_actions=enrollment_actions(
                    enrollment, event=event, actor=actor, on_team=project is not None, now=now
                ),
            )
        )
    return views


async def formation_participation(
    session: AsyncSession, event: models.Event, actor: models.User, now: datetime
) -> schemas.Formation | None:
    if not event.formation_enabled:
        return None
    formation = await session.get(models.EventFormation, event.id)
    if formation is None:
        return None
    settings = await load_settings(session)
    min_size = event.team_size_min or settings.team_size_default_min
    max_size = event.team_size_max or settings.team_size_default_max
    if min_size > max_size:
        max_size = min_size
    if max_size < 2:
        max_size = 2
    target = formation.target_override or max_size
    target = min(max(target, min_size), max_size)
    viable = min(max_size, max(min_size, ceil(target * settings.viable_ratio)))
    core_max = min(max_size, max(2, ceil(target * settings.core_ratio)))
    timing = formation_timing(event, settings, now)
    counts = {
        status: 0
        for status in (EnrollmentStatus.ACTIVE, EnrollmentStatus.PLACED, EnrollmentStatus.RESERVE)
    }
    volunteers = 0
    for status, count, willing in await session.execute(
        select(
            models.Enrollment.status,
            func.count(),
            func.count().filter(models.Enrollment.willing_to_lead),
        )
        .where(
            models.Enrollment.event_id == event.id,
            models.Enrollment.status.in_(
                [EnrollmentStatus.ACTIVE, EnrollmentStatus.PLACED, EnrollmentStatus.RESERVE]
            ),
        )
        .group_by(models.Enrollment.status)
    ):
        counts[EnrollmentStatus(status)] = count
        volunteers += willing
    cores = (
        await session.execute(
            select(models.Project.id, func.count(models.ProjectMember.id))
            .outerjoin(
                models.ProjectMember,
                (models.ProjectMember.project_id == models.Project.id)
                & models.ProjectMember.left_at.is_(None),
            )
            .where(
                models.Project.event_id == event.id,
                models.Project.team_status == TeamStatus.FORMING,
            )
            .group_by(models.Project.id)
            .order_by(models.Project.created_at, models.Project.id)
        )
    ).all()
    teams = (
        await session.scalar(
            select(func.count())
            .select_from(models.Project)
            .where(
                models.Project.event_id == event.id,
                models.Project.team_status.is_not(None),
                models.Project.team_status.not_in([TeamStatus.DISBANDED, TeamStatus.CANCELLED]),
            )
        )
        or 0
    )
    free = (
        await session.scalar(
            select(func.count())
            .select_from(models.Enrollment)
            .where(
                models.Enrollment.event_id == event.id,
                models.Enrollment.status == EnrollmentStatus.ACTIVE,
                ~exists().where(
                    models.ProjectMember.event_id == event.id,
                    models.ProjectMember.user_id == models.Enrollment.user_id,
                    models.ProjectMember.left_at.is_(None),
                ),
            )
        )
        or 0
    )
    phase = FormationPhase(formation.phase)
    if phase == FormationPhase.OPEN:
        cores_sizes = [min(max_size, max(1, count)) for _, count in cores]
        try:
            bounds = placement_bounds(free, cores_sizes, viable, target, max_size)
        except ValueError:
            return None
        projection = schemas.FormationProjection(
            teams=bounds.teams,
            new_teams=bounds.new_teams,
            placed=bounds.placed,
            reserve=bounds.reserve,
            undersized_cores=bounds.undersized_cores,
            team_sizes=projected_team_sizes(
                bounds.placed, cores_sizes, bounds.new_teams, viable, target, max_size
            ),
            volunteers=volunteers,
        )
    else:
        team_rows = (
            await session.execute(
                select(models.Project.id, func.count(models.ProjectMember.id))
                .outerjoin(
                    models.ProjectMember,
                    (models.ProjectMember.project_id == models.Project.id)
                    & models.ProjectMember.left_at.is_(None),
                )
                .where(
                    models.Project.event_id == event.id,
                    models.Project.team_status.is_not(None),
                    models.Project.team_status.not_in(
                        [TeamStatus.DISBANDED.value, TeamStatus.CANCELLED.value]
                    ),
                )
                .group_by(models.Project.id)
                .order_by(models.Project.created_at, models.Project.id)
            )
        ).all()
        projection = schemas.FormationProjection(
            teams=len(team_rows),
            new_teams=0,
            placed=counts[EnrollmentStatus.PLACED],
            reserve=counts[EnrollmentStatus.RESERVE],
            undersized_cores=0,
            team_sizes=[count for _, count in team_rows],
            volunteers=volunteers,
        )
    fit = await session.get(models.LeadFit, (event.id, actor.id))
    membership = await live_membership(session, event.id, actor.id)
    rows = list(
        await session.scalars(
            select(models.Enrollment).where(
                models.Enrollment.event_id == event.id, models.Enrollment.user_id == actor.id
            )
        )
    )
    enrollment = next(
        (row for row in rows if row.status in LIVE_ENROLLMENT_STATES),
        max(rows, key=lambda row: (row.status_at, row.id), default=None),
    )
    views = await enrollment_views(session, [enrollment], actor, now) if enrollment else []
    my_state = schemas.ParticipationState(
        enrollment=views[0] if views else None,
        team=ProjectRef(
            id=membership[1].id,
            name=membership[1].name,
            event_id=membership[1].event_id,
            team_status=TeamStatus(membership[1].team_status),
        )
        if membership
        else None,
        membership=await membership_view(session, membership[0], actor, now)
        if membership
        else None,
        lead_fit=lead_eligibility(fit, settings),
    )
    return schemas.Formation(
        event_id=event.id,
        phase=phase,
        target=target,
        viable=viable,
        min_size=min_size,
        max_size=max_size,
        core_max=core_max,
        target_override=formation.target_override,
        formation_at_override=formation.formation_at_override,
        formation_at=formation.formation_at,
        review_ends_at=formation.review_ends_at,
        team_deadline=timing.team_deadline,
        reg_deadline=timing.reg_deadline,
        rounds=formation.rounds,
        last_run_id=formation.last_run_id,
        counts=schemas.FormationCounts(
            enrollments=sum(counts.values()),
            active=counts[EnrollmentStatus.ACTIVE],
            placed=counts[EnrollmentStatus.PLACED],
            reserve=counts[EnrollmentStatus.RESERVE],
            cores=len(cores),
            core_members=sum(count for _, count in cores),
            teams=teams,
            volunteers=volunteers,
        ),
        projection=projection,
        my_state=my_state,
        allowed_actions=formation_actions(
            event,
            formation,
            actor=actor,
            enrollment=enrollment,
            on_team=membership is not None,
            lead_fit=fit,
            now=now,
            reg_deadline=timing.reg_deadline,
            settings=settings,
        ),
    )


async def resolve_preferred_area(session: AsyncSession, key: str | None) -> UUID | None:
    if key is None:
        return None
    area_id = await session.scalar(select(models.Area.id).where(models.Area.key == key))
    if area_id is None:
        raise ProblemError(ErrorCode.VALIDATION_ERROR, f"Unknown area: {key}")
    return area_id


async def enroll(
    session: AsyncSession,
    jobs: procrastinate.App,
    event_id: UUID,
    body: schemas.CreateEnrollmentBody,
    actor: models.User,
    now: datetime,
) -> tuple[models.Enrollment, bool]:
    event, phase = await participation_event(session, event_id, actor)
    settings = await load_settings(session)
    timing = formation_timing(event, settings, now)
    if (
        phase == FormationPhase.CLOSED
        or event.status != EventStatus.PUBLISHED
        or now >= timing.reg_deadline
        or now >= event.starts_at
    ):
        raise ProblemError(ErrorCode.ENROLLMENT_CLOSED, "Enrollment is closed")
    if await live_membership(session, event_id, actor.id) is not None:
        raise ProblemError(ErrorCode.ALREADY_IN_TEAM, "You are already on a team for this event")
    area_id = await resolve_preferred_area(session, body.preferred_area_key)
    current = await live_enrollment_row(session, event_id, actor.id, for_update=True)
    if current is not None:
        if (current.preferred_area_id, current.note, current.willing_to_lead) == (
            area_id,
            body.note,
            body.willing_to_lead,
        ):
            return current, False
        raise ProblemError(ErrorCode.ALREADY_ENROLLED, "You are already enrolled in this event")
    enrollment = await latest_terminal_enrollment(session, event_id, actor.id, for_update=True)
    if enrollment is None:
        enrollment = models.Enrollment(
            event_id=event_id, user_id=actor.id, source=EnrollmentSource.SELF.value
        )
        session.add(enrollment)
    enrollment.preferred_area_id = area_id
    enrollment.note = body.note
    enrollment.willing_to_lead = body.willing_to_lead
    enrollment.status = (
        EnrollmentStatus.ACTIVE if phase == FormationPhase.OPEN else EnrollmentStatus.RESERVE
    ).value
    enrollment.status_at = now
    enrollment.reserve_rank = None
    await attendance.sync_going(session, event_id, actor.id, going=True, now=now)
    await enqueue_participation_jobs(
        session, jobs, event_id, reserve=enrollment.status == EnrollmentStatus.RESERVE.value
    )
    await session.flush()
    await session.refresh(enrollment)
    return enrollment, True


async def update_enrollment(
    session: AsyncSession,
    event_id: UUID,
    body: schemas.UpdateEnrollmentBody,
    actor: models.User,
    now: datetime,
) -> models.Enrollment:
    event, _ = await participation_event(session, event_id, actor)
    if now >= event.starts_at:
        raise ProblemError(ErrorCode.ENROLLMENT_CLOSED, "Enrollment edits close at the event start")
    row = await live_enrollment_row(session, event_id, actor.id, for_update=True)
    if row is None:
        raise ProblemError(ErrorCode.NOT_ENROLLED, "You are not enrolled in this event")
    changes = body.model_dump(exclude_unset=True)
    if "preferred_area_key" in changes:
        changes["preferred_area_id"] = await resolve_preferred_area(
            session, changes.pop("preferred_area_key")
        )
    if changes.get("willing_to_lead", False) is None:
        del changes["willing_to_lead"]
    for field, value in changes.items():
        if getattr(row, field) != value:
            setattr(row, field, value)
    await session.flush()
    await session.refresh(row)
    return row


async def withdraw_enrollment(
    session: AsyncSession,
    jobs: procrastinate.App,
    event_id: UUID,
    actor: models.User,
    now: datetime,
) -> None:
    event, _ = await participation_event(session, event_id, actor)
    if now >= event.starts_at:
        raise ProblemError(ErrorCode.ENROLLMENT_CLOSED, "Enrollment is closed")
    if await live_membership(session, event_id, actor.id) is not None:
        raise ProblemError(ErrorCode.ALREADY_IN_TEAM, "Leave the team first")
    row = await live_enrollment_row(session, event_id, actor.id, for_update=True)
    if row is None:
        return None
    reserve = row.status == EnrollmentStatus.RESERVE.value
    row.status = EnrollmentStatus.WITHDRAWN.value
    row.status_at = now
    row.reserve_rank = None
    await attendance.sync_going(session, event_id, actor.id, going=False, now=now)
    await enqueue_participation_jobs(session, jobs, event_id, reserve=reserve)
    await session.flush()
    return None


async def list_enrollments(
    session: AsyncSession,
    event_id: UUID,
    actor: models.User,
    pagination: CursorParams,
    now: datetime,
) -> schemas.EnrollmentPage:
    event = await formation_event(session, event_id, actor)
    if not event.formation_enabled:
        raise ProblemError(ErrorCode.NOT_FOUND)
    statement = select(models.Enrollment).where(
        models.Enrollment.event_id == event_id,
        models.Enrollment.status.in_(sorted(member.value for member in LIVE_ENROLLMENT_STATES)),
    )
    page = await keyset_page(
        session,
        statement,
        pagination,
        sort=[models.Enrollment.created_at],
        id_column=models.Enrollment.id,
        serialize=lambda row: row,
        descending=False,
    )
    return schemas.EnrollmentPage(
        items=await enrollment_views(session, page.items, actor, now),
        next_cursor=page.next_cursor,
    )


async def list_my_enrollments(
    session: AsyncSession, actor: models.User, status: EnrollmentStatus | None, now: datetime
) -> list[schemas.Enrollment]:
    statement = select(models.Enrollment).where(models.Enrollment.user_id == actor.id)
    if status is not None:
        statement = statement.where(models.Enrollment.status == status.value)
    statement = statement.order_by(models.Enrollment.status_at.desc(), models.Enrollment.id.desc())
    return await enrollment_views(session, list(await session.scalars(statement)), actor, now)


async def withdraw_user_enrollments(
    session: AsyncSession, user_id: UUID, now: datetime
) -> list[UUID]:
    live = sorted(member.value for member in LIVE_ENROLLMENT_STATES)
    event_ids = list(
        await session.scalars(
            select(models.Enrollment.event_id)
            .where(models.Enrollment.user_id == user_id, models.Enrollment.status.in_(live))
            .distinct()
            .order_by(models.Enrollment.event_id)
        )
    )
    if not event_ids:
        return []
    for event_id in event_ids:
        await session.execute(
            select(models.Event).where(models.Event.id == event_id).with_for_update()
        )
    await session.execute(
        update(models.Enrollment)
        .where(
            models.Enrollment.user_id == user_id,
            models.Enrollment.event_id.in_(event_ids),
            models.Enrollment.status.in_(live),
        )
        .values(status=EnrollmentStatus.WITHDRAWN.value, status_at=now, reserve_rank=None)
    )
    for event_id in event_ids:
        await attendance.sync_going(session, event_id, user_id, going=False, now=now)
    await session.flush()
    return event_ids
