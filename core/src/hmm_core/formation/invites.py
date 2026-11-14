from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import UUID

import procrastinate
from hmm_contracts.common import (
    EnrollmentSource,
    EnrollmentStatus,
    ErrorCode,
    FormationPhase,
    InviteKind,
    InviteStatus,
    JoinedVia,
    MemberRole,
    MemberState,
    ProjectStatus,
    ProjectVisibility,
    TeamStatus,
    Trust,
)
from hmm_contracts.notifications import (
    ActionKey,
    ApiAction,
    InviteAcceptedPayload,
    InviteClosedPayload,
    InviteDeclinedPayload,
    InviteReceivedPayload,
    ProjectInviteReceivedPayload,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.formation.common import (
    ACCEPT_LABEL,
    DECLINE_LABEL,
    ONBOARDED_STATUSES,
    dnd_active,
    enqueue_participation_jobs,
    formation_event,
    formation_row,
    formation_timing,
    latest_terminal_enrollment,
    live_enrollment_row,
    locked_formation_event,
    notification_person,
)
from hmm_core.formation.enrollments import live_membership, resolve_preferred_area
from hmm_core.formation.leads import close_event_pending_items, formation_sizes
from hmm_core.notifications.service import notify
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.settings import load_settings
from hmm_core.projects import schemas as project_schemas
from hmm_core.projects import service as projects


async def invite_project(
    session: AsyncSession, project_id: UUID, actor: models.User, *, lock: bool
) -> tuple[models.Project, models.Event | None]:
    project = await session.get(models.Project, project_id)
    if project is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    event: models.Event | None = None
    if project.team_status is not None and project.event_id is not None:
        if lock:
            event = await locked_formation_event(session, project.event_id, actor)
        else:
            event = await formation_event(session, project.event_id, actor)
    if lock:
        project = await session.scalar(
            select(models.Project)
            .where(models.Project.id == project_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if project is None:
            raise ProblemError(ErrorCode.NOT_FOUND)
    if project.team_status is None:
        live = await session.scalar(
            select(models.ProjectMember.id)
            .where(
                models.ProjectMember.project_id == project_id,
                models.ProjectMember.user_id == actor.id,
                models.ProjectMember.left_at.is_(None),
            )
            .limit(1)
        )
        if (
            project.visibility == ProjectVisibility.MEMBERS.value
            and not actor.is_admin
            and actor.id != project.lead_user_id
            and live is None
        ):
            raise ProblemError(ErrorCode.NOT_FOUND)
    return project, event


async def invite_context(
    session: AsyncSession, invite_id: UUID, actor: models.User
) -> tuple[models.Invite, models.Project, models.Event | None, models.EventFormation | None]:
    invite = await session.get(models.Invite, invite_id)
    if invite is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    project = await session.get(models.Project, invite.project_id)
    if project is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    event: models.Event | None = None
    formation: models.EventFormation | None = None
    if project.team_status is not None and project.event_id is not None:
        event = await locked_formation_event(session, project.event_id, actor)
        formation = await formation_row(session, event.id)
    project = await session.scalar(
        select(models.Project)
        .where(models.Project.id == invite.project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if project is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    invite = await session.scalar(
        select(models.Invite)
        .where(models.Invite.id == invite_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if invite is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return invite, project, event, formation


async def invite_views(
    session: AsyncSession, invites: Sequence[models.Invite], actor: models.User
) -> list[project_schemas.Invite]:
    if not invites:
        return []
    project_ids = {invite.project_id for invite in invites}
    user_ids = {invite.user_id for invite in invites} | {
        invite.invited_by for invite in invites if invite.invited_by is not None
    }
    area_ids = {invite.area_id for invite in invites if invite.area_id is not None}
    projects_by_id = {
        project.id: project
        for project in await session.scalars(
            select(models.Project).where(models.Project.id.in_(project_ids))
        )
    }
    users: dict[UUID, models.User] = {
        user.id: user
        for user in await session.scalars(select(models.User).where(models.User.id.in_(user_ids)))
    }
    area_keys: dict[UUID, str] = (
        {
            area.id: area.key
            for area in await session.scalars(
                select(models.Area).where(models.Area.id.in_(area_ids))
            )
        }
        if area_ids
        else {}
    )
    return [
        projects.invite_view(
            projects_by_id[invite.project_id],
            invite,
            users=users,
            area_keys=area_keys,
            actor=actor,
        )
        for invite in invites
    ]


async def late_core_open(
    session: AsyncSession,
    event: models.Event,
    formation: models.EventFormation,
    project: models.Project,
    now: datetime,
) -> bool:
    return False


async def create_invite(
    session: AsyncSession,
    project_id: UUID,
    body: project_schemas.CreateProjectInviteBody,
    actor: models.User,
    now: datetime,
) -> models.Invite:
    project, event = await invite_project(session, project_id, actor, lock=True)
    if project.lead_user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    invitee = await session.get(models.User, body.user_id)
    if invitee is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if invitee.onboarding_status not in ONBOARDED_STATUSES:
        raise ProblemError(ErrorCode.INVALID_STATE, "Invitee is not an onboarded member")
    if invitee.banned_at is not None:
        raise ProblemError(ErrorCode.INVALID_STATE, "Invitee is banned")
    if dnd_active(invitee, now):
        raise ProblemError(ErrorCode.USER_DND, "This member has do-not-disturb enabled")
    settings = await load_settings(session)
    area_id = await resolve_preferred_area(session, body.area_key)
    existing = await session.scalar(
        select(models.Invite.id).where(
            models.Invite.project_id == project.id,
            models.Invite.user_id == body.user_id,
            models.Invite.status == InviteStatus.PENDING.value,
        )
    )
    if existing is not None:
        raise ProblemError(ErrorCode.ALREADY_INVITED, "This member already has a pending invite")
    ttl = timedelta(days=settings.invite_ttl_days)
    if event is not None:
        formation = await formation_row(session, event.id)
        phase = FormationPhase(formation.phase)
        sizes = formation_sizes(event, formation, settings)
        if await live_membership(session, event.id, body.user_id) is not None:
            raise ProblemError(
                ErrorCode.ALREADY_IN_TEAM, "The invitee is already on a team for this event"
            )
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
        if phase == FormationPhase.FORMING:
            raise ProblemError(
                ErrorCode.FORMATION_IN_PROGRESS, "Formation is running, retry shortly"
            )
        if project.team_status == TeamStatus.FORMING.value:
            if phase != FormationPhase.OPEN and not await late_core_open(
                session, event, formation, project, now
            ):
                raise ProblemError(ErrorCode.INVALID_STATE, "Core invites are closed")
            if phase == FormationPhase.OPEN and now >= formation.formation_at:
                raise ProblemError(
                    ErrorCode.INVALID_STATE, "Core invites close at the formation time"
                )
            kind = InviteKind.CORE.value
            pending = (
                await session.scalar(
                    select(func.count())
                    .select_from(models.Invite)
                    .where(
                        models.Invite.project_id == project.id,
                        models.Invite.kind == InviteKind.CORE.value,
                        models.Invite.status == InviteStatus.PENDING.value,
                    )
                )
                or 0
            )
            if pending >= sizes.core_max - live_count:
                raise ProblemError(
                    ErrorCode.CORE_FULL,
                    f"Core is full: {live_count} of {sizes.core_max} members"
                    f" with {pending} pending invites",
                )
            expires_at = min(now + ttl, formation.formation_at)
        elif project.team_status in (TeamStatus.REVIEW.value, TeamStatus.LOCKED.value):
            timing = formation_timing(event, settings, now)
            if now >= timing.reg_deadline:
                raise ProblemError(
                    ErrorCode.ENROLLMENT_CLOSED, "Fill invites close at the registration deadline"
                )
            if live_count >= sizes.max_size:
                raise ProblemError(
                    ErrorCode.TEAM_FULL, f"Team has reached max size {sizes.max_size}"
                )
            kind = InviteKind.FILL.value
            expires_at = min(now + ttl, timing.reg_deadline)
        else:
            raise ProblemError(ErrorCode.INVALID_STATE, "This team is not accepting invites")
    else:
        if project.status in (
            ProjectStatus.ABANDONED.value,
            ProjectStatus.DISBANDED.value,
            ProjectStatus.CANCELLED.value,
        ):
            raise ProblemError(ErrorCode.INVALID_STATE, "This project is closed")
        member = await session.scalar(
            select(models.ProjectMember.id).where(
                models.ProjectMember.project_id == project.id,
                models.ProjectMember.user_id == body.user_id,
                models.ProjectMember.left_at.is_(None),
            )
        )
        if member is not None:
            raise ProblemError(
                ErrorCode.INVALID_STATE, "The invitee is already a member of this project"
            )
        kind = InviteKind.PROJECT.value
        expires_at = now + ttl
    invite = models.Invite(
        project_id=project.id,
        event_id=event.id if event is not None else None,
        user_id=body.user_id,
        invited_by=actor.id,
        kind=kind,
        message=body.message,
        area_id=area_id,
        status=InviteStatus.PENDING.value,
        closed_reason=None,
        expires_at=expires_at,
        responded_at=None,
    )
    session.add(invite)
    await session.flush()
    project.last_lead_action_at = now
    await session.flush()
    actions = (
        ApiAction(key=ActionKey.INVITE_ACCEPT, entity_id=invite.id, label_key=ACCEPT_LABEL),
        ApiAction(key=ActionKey.INVITE_DECLINE, entity_id=invite.id, label_key=DECLINE_LABEL),
    )
    if event is not None:
        await notify(
            session,
            invitee.id,
            InviteReceivedPayload(
                link=f"/projects/{project.id}",
                actions=actions,
                event_id=event.id,
                event_title=event.title,
                project_id=project.id,
                team_name=project.name,
                invite_id=invite.id,
                kind=InviteKind.CORE if kind == InviteKind.CORE.value else InviteKind.FILL,
                actor=notification_person(actor),
                invitee=notification_person(invitee),
                expires_at=expires_at,
                message=body.message,
                area_key=body.area_key,
            ),
            now,
            dedupe_key=f"invite_received:{invite.id}",
        )
    else:
        await notify(
            session,
            invitee.id,
            ProjectInviteReceivedPayload(
                link=f"/projects/{project.id}",
                actions=actions,
                project_id=project.id,
                project_name=project.name,
                invite_id=invite.id,
                actor=notification_person(actor),
                expires_at=expires_at,
                message=body.message,
                area_key=body.area_key,
            ),
            now,
            dedupe_key=f"project_invite_received:{invite.id}",
        )
    return invite


async def list_invites(
    session: AsyncSession, project_id: UUID, actor: models.User
) -> list[project_schemas.Invite]:
    project, _ = await invite_project(session, project_id, actor, lock=False)
    if not actor.is_admin and actor.id != project.lead_user_id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    invites = await session.scalars(
        select(models.Invite)
        .where(models.Invite.project_id == project.id)
        .order_by(models.Invite.created_at.desc(), models.Invite.id.desc())
    )
    return await invite_views(session, list(invites), actor)


async def list_my_invites(
    session: AsyncSession, status: InviteStatus | None, actor: models.User
) -> list[project_schemas.Invite]:
    statement = select(models.Invite).where(models.Invite.user_id == actor.id)
    if status is not None:
        statement = statement.where(models.Invite.status == status.value)
    statement = statement.order_by(models.Invite.created_at.desc(), models.Invite.id.desc())
    return await invite_views(session, list(await session.scalars(statement)), actor)


async def close_invitee_items(
    session: AsyncSession,
    event: models.Event,
    invitee: models.User,
    now: datetime,
    *,
    closed_reason: str,
    notified_reason: str,
    keep: UUID | None,
) -> None:
    statement = select(models.Invite).where(
        models.Invite.event_id == event.id,
        models.Invite.user_id == invitee.id,
        models.Invite.status == InviteStatus.PENDING.value,
    )
    if keep is not None:
        statement = statement.where(models.Invite.id != keep)
    closing = list(await session.scalars(statement))
    await close_event_pending_items(session, event.id, invitee.id, now, reason=closed_reason)
    if not closing:
        return None
    projects_by_id = {
        project.id: project
        for project in await session.scalars(
            select(models.Project).where(
                models.Project.id.in_({invite.project_id for invite in closing})
            )
        )
    }
    for invite in closing:
        project = projects_by_id[invite.project_id]
        recipient = project.lead_user_id or invite.invited_by
        if recipient is None:
            continue
        await notify(
            session,
            recipient,
            InviteClosedPayload(
                link=f"/projects/{invite.project_id}",
                project_id=invite.project_id,
                project_name=project.name,
                invite_id=invite.id,
                kind=InviteKind(invite.kind),
                actor=notification_person(invitee),
                invitee=notification_person(invitee),
                expires_at=invite.expires_at,
                event_id=event.id,
                event_title=event.title,
                reason=notified_reason,
            ),
            now,
            dedupe_key=f"invite_closed:{invite.id}",
        )
    return None


async def accept_invite(
    session: AsyncSession,
    jobs: procrastinate.App,
    invite_id: UUID,
    actor: models.User,
    now: datetime,
) -> models.Invite:
    invite, project, event, formation = await invite_context(session, invite_id, actor)
    if actor.id != invite.user_id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if invite.status == InviteStatus.ACCEPTED.value:
        return invite
    if invite.status != InviteStatus.PENDING.value:
        raise ProblemError(ErrorCode.INVALID_STATE, "This invite is no longer pending")
    if now >= invite.expires_at:
        raise ProblemError(ErrorCode.INVALID_STATE, "This invite has expired")
    settings = await load_settings(session)
    live_count_query = (
        select(func.count())
        .select_from(models.ProjectMember)
        .where(
            models.ProjectMember.project_id == project.id,
            models.ProjectMember.left_at.is_(None),
        )
    )
    if event is not None and formation is not None:
        if FormationPhase(formation.phase) == FormationPhase.FORMING:
            raise ProblemError(
                ErrorCode.FORMATION_IN_PROGRESS, "Formation is running, retry shortly"
            )
        if project.team_status not in (
            TeamStatus.FORMING.value,
            TeamStatus.REVIEW.value,
            TeamStatus.LOCKED.value,
        ):
            raise ProblemError(ErrorCode.INVALID_STATE, "This team is no longer accepting members")
        if await live_membership(session, event.id, actor.id) is not None:
            raise ProblemError(
                ErrorCode.ALREADY_IN_TEAM, "You are already on a team for this event"
            )
        sizes = formation_sizes(event, formation, settings)
        live_count = await session.scalar(live_count_query) or 0
        if live_count >= sizes.max_size:
            raise ProblemError(ErrorCode.TEAM_FULL, f"Team has reached max size {sizes.max_size}")
    else:
        if project.status in (
            ProjectStatus.ABANDONED.value,
            ProjectStatus.DISBANDED.value,
            ProjectStatus.CANCELLED.value,
        ):
            raise ProblemError(ErrorCode.INVALID_STATE, "This project is closed")
        member = await session.scalar(
            select(models.ProjectMember.id).where(
                models.ProjectMember.project_id == project.id,
                models.ProjectMember.user_id == actor.id,
                models.ProjectMember.left_at.is_(None),
            )
        )
        if member is not None:
            raise ProblemError(ErrorCode.INVALID_STATE, "You are already a member of this project")
        member_count = await session.scalar(live_count_query) or 0
        if member_count >= settings.project_size_max:
            raise ProblemError(
                ErrorCode.PROJECT_FULL, f"Project has reached max size {settings.project_size_max}"
            )
    invite.status = InviteStatus.ACCEPTED.value
    invite.responded_at = now
    joined_via = {
        InviteKind.CORE.value: JoinedVia.CORE_INVITE.value,
        InviteKind.FILL.value: JoinedVia.FILL_INVITE.value,
    }.get(invite.kind, JoinedVia.INVITE.value)
    session.add(
        models.ProjectMember(
            project_id=project.id,
            event_id=event.id if event is not None else None,
            user_id=actor.id,
            role=MemberRole.MEMBER.value,
            area_id=invite.area_id,
            state=MemberState.CONFIRMED.value,
            trust=Trust.SELF.value,
            joined_via=joined_via,
            proposal_reason=None,
            confirm_by=None,
            confirm_kind=None,
            is_team_row=event is not None,
            joined_at=now,
            confirmed_at=now,
        )
    )
    reserve = False
    if event is not None:
        enrollment = await live_enrollment_row(session, event.id, actor.id, for_update=True)
        if enrollment is None:
            enrollment = await latest_terminal_enrollment(
                session, event.id, actor.id, for_update=True
            )
        if enrollment is None:
            enrollment = models.Enrollment(
                event_id=event.id,
                user_id=actor.id,
                source=EnrollmentSource.INVITE.value,
                willing_to_lead=False,
            )
            session.add(enrollment)
        else:
            reserve = enrollment.status == EnrollmentStatus.RESERVE.value
        enrollment.status = EnrollmentStatus.PLACED.value
        enrollment.status_at = now
        enrollment.reserve_rank = None
    await session.flush()
    if event is not None:
        await close_invitee_items(
            session,
            event,
            actor,
            now,
            closed_reason="joined_team",
            notified_reason="The invitee joined another team",
            keep=invite.id,
        )
    recipient = project.lead_user_id or invite.invited_by
    if recipient is not None:
        await notify(
            session,
            recipient,
            InviteAcceptedPayload(
                link=f"/projects/{project.id}",
                project_id=project.id,
                project_name=project.name,
                invite_id=invite.id,
                kind=InviteKind(invite.kind),
                actor=notification_person(actor),
                invitee=notification_person(actor),
                expires_at=invite.expires_at,
                event_id=event.id if event is not None else None,
                event_title=event.title if event is not None else None,
            ),
            now,
            dedupe_key=f"invite_accepted:{invite.id}",
        )
    if event is not None:
        await enqueue_participation_jobs(session, jobs, event.id, reserve=reserve)
    await session.flush()
    await session.refresh(invite)
    return invite


async def decline_invite(
    session: AsyncSession, invite_id: UUID, actor: models.User, now: datetime
) -> models.Invite:
    invite, project, event, formation = await invite_context(session, invite_id, actor)
    if actor.id != invite.user_id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if invite.status == InviteStatus.DECLINED.value:
        return invite
    if invite.status != InviteStatus.PENDING.value:
        raise ProblemError(ErrorCode.INVALID_STATE, "This invite is no longer pending")
    if now >= invite.expires_at:
        raise ProblemError(ErrorCode.INVALID_STATE, "This invite has expired")
    if event is not None and formation is not None:
        if FormationPhase(formation.phase) == FormationPhase.FORMING:
            raise ProblemError(
                ErrorCode.FORMATION_IN_PROGRESS, "Formation is running, retry shortly"
            )
        if project.team_status not in (
            TeamStatus.FORMING.value,
            TeamStatus.REVIEW.value,
            TeamStatus.LOCKED.value,
        ):
            raise ProblemError(ErrorCode.INVALID_STATE, "This team is no longer accepting members")
    elif project.status in (
        ProjectStatus.ABANDONED.value,
        ProjectStatus.DISBANDED.value,
        ProjectStatus.CANCELLED.value,
    ):
        raise ProblemError(ErrorCode.INVALID_STATE, "This project is closed")
    invite.status = InviteStatus.DECLINED.value
    invite.responded_at = now
    recipient = project.lead_user_id or invite.invited_by
    if recipient is not None:
        await notify(
            session,
            recipient,
            InviteDeclinedPayload(
                link=f"/projects/{project.id}",
                project_id=project.id,
                project_name=project.name,
                invite_id=invite.id,
                kind=InviteKind(invite.kind),
                actor=notification_person(actor),
                invitee=notification_person(actor),
                expires_at=invite.expires_at,
                event_id=event.id if event is not None else None,
                event_title=event.title if event is not None else None,
            ),
            now,
            dedupe_key=f"invite_declined:{invite.id}",
        )
    await session.flush()
    await session.refresh(invite)
    return invite


async def withdraw_invite(
    session: AsyncSession, invite_id: UUID, actor: models.User, now: datetime
) -> None:
    invite, project, event, formation = await invite_context(session, invite_id, actor)
    if project.lead_user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if invite.status == InviteStatus.WITHDRAWN.value:
        return None
    if invite.status != InviteStatus.PENDING.value:
        raise ProblemError(ErrorCode.INVALID_STATE, "This invite is no longer pending")
    if formation is not None and FormationPhase(formation.phase) == FormationPhase.FORMING:
        raise ProblemError(ErrorCode.FORMATION_IN_PROGRESS, "Formation is running, retry shortly")
    invite.status = InviteStatus.WITHDRAWN.value
    invite.responded_at = now
    project.last_lead_action_at = now
    invitee = await session.get(models.User, invite.user_id)
    if invitee is not None:
        await notify(
            session,
            invite.user_id,
            InviteClosedPayload(
                link=f"/projects/{project.id}",
                project_id=project.id,
                project_name=project.name,
                invite_id=invite.id,
                kind=InviteKind(invite.kind),
                actor=notification_person(actor),
                invitee=notification_person(invitee),
                expires_at=invite.expires_at,
                event_id=event.id if event is not None else None,
                event_title=event.title if event is not None else None,
                reason="The lead withdrew the invite",
            ),
            now,
            dedupe_key=f"invite_closed:{invite.id}",
        )
    await session.flush()
    return None
