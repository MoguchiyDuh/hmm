from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

import procrastinate
from hmm_contracts.common import (
    EnrollmentStatus,
    ErrorCode,
    FormationPhase,
    InviteStatus,
    JoinedVia,
    JoinRequestStatus,
    MemberRole,
    MemberState,
    ProjectStatus,
    TeamStatus,
    Trust,
)
from hmm_contracts.notifications import (
    ActionKey,
    ApiAction,
    JoinRequestAcceptedPayload,
    JoinRequestReceivedPayload,
    JoinRequestRejectedPayload,
    ProjectJoinRequestReceivedPayload,
    ProjectMemberJoinedPayload,
)
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.formation.common import (
    ACCEPT_LABEL,
    REJECT_LABEL,
    enqueue_participation_jobs,
    formation_row,
    live_enrollment_row,
    locked_formation_event,
    notification_person,
)
from hmm_core.formation.enrollments import live_membership
from hmm_core.formation.invites import close_invitee_items, invite_project, late_core_open
from hmm_core.formation.leads import formation_sizes
from hmm_core.notifications.service import notify, notify_many
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.settings import load_settings
from hmm_core.projects import schemas as project_schemas
from hmm_core.projects import service as projects


async def join_request_context(
    session: AsyncSession, join_request_id: UUID, actor: models.User
) -> tuple[models.JoinRequest, models.Project, models.Event | None, models.EventFormation | None]:
    request = await session.get(models.JoinRequest, join_request_id)
    if request is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    project = await session.get(models.Project, request.project_id)
    if project is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    event: models.Event | None = None
    formation: models.EventFormation | None = None
    if project.team_status is not None and project.event_id is not None:
        event = await locked_formation_event(session, project.event_id, actor)
        formation = await formation_row(session, event.id)
    project = await session.scalar(
        select(models.Project)
        .where(models.Project.id == request.project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if project is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    request = await session.scalar(
        select(models.JoinRequest)
        .where(models.JoinRequest.id == join_request_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if request is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return request, project, event, formation


async def join_request_views(
    session: AsyncSession, requests: Sequence[models.JoinRequest], actor: models.User
) -> list[project_schemas.JoinRequest]:
    if not requests:
        return []
    projects_by_id = {
        project.id: project
        for project in await session.scalars(
            select(models.Project).where(
                models.Project.id.in_({request.project_id for request in requests})
            )
        )
    }
    users: dict[UUID, models.User] = {
        user.id: user
        for user in await session.scalars(
            select(models.User).where(models.User.id.in_({request.user_id for request in requests}))
        )
    }
    return [
        projects.join_request_view(
            projects_by_id[request.project_id], request, users=users, actor=actor
        )
        for request in requests
    ]


async def create_join_request(
    session: AsyncSession,
    project_id: UUID,
    body: project_schemas.CreateProjectJoinRequestBody,
    actor: models.User,
    now: datetime,
) -> models.JoinRequest:
    project, event = await invite_project(session, project_id, actor, lock=True)
    if event is not None:
        formation = await formation_row(session, event.id)
        phase = FormationPhase(formation.phase)
        if phase == FormationPhase.FORMING:
            raise ProblemError(
                ErrorCode.FORMATION_IN_PROGRESS, "Formation is running, retry shortly"
            )
        if project.team_status != TeamStatus.FORMING.value:
            raise ProblemError(
                ErrorCode.INVALID_STATE, "Join requests are only accepted by a forming core"
            )
        if project.lead_user_id is None:
            raise ProblemError(ErrorCode.INVALID_STATE, "This team has no lead")
        if await live_membership(session, event.id, actor.id) is not None:
            raise ProblemError(
                ErrorCode.ALREADY_IN_TEAM, "You are already on a team for this event"
            )
        enrollment = await live_enrollment_row(session, event.id, actor.id, for_update=True)
        if enrollment is None:
            raise ProblemError(
                ErrorCode.NOT_ENROLLED, "Enroll in the hackathon before requesting to join a core"
            )
        late = await late_core_open(session, event, formation, project, now)
        if enrollment.status == EnrollmentStatus.RESERVE.value and not late:
            raise ProblemError(ErrorCode.NOT_ENROLLED, "An active enrollment is required")
        if not late and now >= formation.formation_at:
            raise ProblemError(ErrorCode.INVALID_STATE, "Join requests close at the formation time")
        duplicate = await session.scalar(
            select(models.JoinRequest.id).where(
                models.JoinRequest.event_id == event.id,
                models.JoinRequest.user_id == actor.id,
                models.JoinRequest.status == JoinRequestStatus.PENDING.value,
            )
        )
        if duplicate is not None:
            raise ProblemError(
                ErrorCode.JOIN_REQUEST_PENDING,
                "You already have a pending join request for this hackathon",
            )
    else:
        if project.status in (
            ProjectStatus.ABANDONED.value,
            ProjectStatus.DISBANDED.value,
            ProjectStatus.CANCELLED.value,
        ):
            raise ProblemError(ErrorCode.INVALID_STATE, "This project is closed")
        if project.recruiting is None:
            raise ProblemError(ErrorCode.INVALID_STATE, "This project is not recruiting")
        if project.lead_user_id is None:
            raise ProblemError(ErrorCode.INVALID_STATE, "This project has no lead")
        member = await session.scalar(
            select(models.ProjectMember.id).where(
                models.ProjectMember.project_id == project.id,
                models.ProjectMember.user_id == actor.id,
                models.ProjectMember.left_at.is_(None),
            )
        )
        if member is not None:
            raise ProblemError(ErrorCode.INVALID_STATE, "You are already a member of this project")
        duplicate = await session.scalar(
            select(models.JoinRequest.id).where(
                models.JoinRequest.project_id == project.id,
                models.JoinRequest.event_id.is_(None),
                models.JoinRequest.user_id == actor.id,
                models.JoinRequest.status == JoinRequestStatus.PENDING.value,
            )
        )
        if duplicate is not None:
            raise ProblemError(
                ErrorCode.JOIN_REQUEST_PENDING,
                "You already have a pending join request for this project",
            )
    request = models.JoinRequest(
        project_id=project.id,
        event_id=event.id if event is not None else None,
        user_id=actor.id,
        note=body.note,
        status=JoinRequestStatus.PENDING.value,
        responded_at=None,
    )
    session.add(request)
    await session.flush()
    lead = (
        await session.get(models.User, project.lead_user_id)
        if project.lead_user_id is not None
        else None
    )
    if lead is not None:
        actions = (
            ApiAction(
                key=ActionKey.JOIN_REQUEST_ACCEPT, entity_id=request.id, label_key=ACCEPT_LABEL
            ),
            ApiAction(
                key=ActionKey.JOIN_REQUEST_REJECT, entity_id=request.id, label_key=REJECT_LABEL
            ),
        )
        if event is not None:
            await notify(
                session,
                lead.id,
                JoinRequestReceivedPayload(
                    link=f"/projects/{project.id}",
                    actions=actions,
                    event_id=event.id,
                    event_title=event.title,
                    project_id=project.id,
                    team_name=project.name,
                    join_request_id=request.id,
                    requester=notification_person(actor),
                    lead=notification_person(lead),
                    note=body.note,
                ),
                now,
                dedupe_key=f"join_request_received:{request.id}",
            )
        else:
            await notify(
                session,
                lead.id,
                ProjectJoinRequestReceivedPayload(
                    link=f"/projects/{project.id}",
                    actions=actions,
                    project_id=project.id,
                    project_name=project.name,
                    join_request_id=request.id,
                    requester=notification_person(actor),
                    note=body.note,
                ),
                now,
                dedupe_key=f"project_join_request_received:{request.id}",
            )
    return request


async def list_join_requests(
    session: AsyncSession, project_id: UUID, actor: models.User
) -> list[project_schemas.JoinRequest]:
    project, _ = await invite_project(session, project_id, actor, lock=False)
    if not actor.is_admin and actor.id != project.lead_user_id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    requests = await session.scalars(
        select(models.JoinRequest)
        .where(models.JoinRequest.project_id == project.id)
        .order_by(models.JoinRequest.created_at.desc(), models.JoinRequest.id.desc())
    )
    return await join_request_views(session, list(requests), actor)


async def withdraw_join_request(
    session: AsyncSession, join_request_id: UUID, actor: models.User, now: datetime
) -> None:
    request, _, _, formation = await join_request_context(session, join_request_id, actor)
    if request.user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if request.status == JoinRequestStatus.WITHDRAWN.value:
        return None
    if request.status != JoinRequestStatus.PENDING.value:
        raise ProblemError(ErrorCode.INVALID_STATE, "This join request is no longer pending")
    if formation is not None and FormationPhase(formation.phase) == FormationPhase.FORMING:
        raise ProblemError(ErrorCode.FORMATION_IN_PROGRESS, "Formation is running, retry shortly")
    request.status = JoinRequestStatus.WITHDRAWN.value
    request.responded_at = now
    await session.flush()
    return None


async def accept_join_request(
    session: AsyncSession,
    jobs: procrastinate.App,
    join_request_id: UUID,
    actor: models.User,
    now: datetime,
) -> models.JoinRequest:
    request, project, event, formation = await join_request_context(session, join_request_id, actor)
    if project.lead_user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if request.status == JoinRequestStatus.ACCEPTED.value:
        return request
    if request.status != JoinRequestStatus.PENDING.value:
        raise ProblemError(ErrorCode.INVALID_STATE, "This join request is no longer pending")
    settings = await load_settings(session)
    live_count_query = (
        select(func.count())
        .select_from(models.ProjectMember)
        .where(
            models.ProjectMember.project_id == project.id,
            models.ProjectMember.left_at.is_(None),
        )
    )
    enrollment: models.Enrollment | None = None
    if event is not None and formation is not None:
        if FormationPhase(formation.phase) == FormationPhase.FORMING:
            raise ProblemError(
                ErrorCode.FORMATION_IN_PROGRESS, "Formation is running, retry shortly"
            )
        if project.team_status != TeamStatus.FORMING.value:
            raise ProblemError(
                ErrorCode.INVALID_STATE, "Join requests are only accepted by a forming core"
            )
        requester = await session.get(models.User, request.user_id)
        if requester is None:
            raise ProblemError(ErrorCode.NOT_FOUND)
        if await live_membership(session, event.id, request.user_id) is not None:
            raise ProblemError(
                ErrorCode.INVALID_STATE, "The requester is already on a team for this event"
            )
        enrollment = await live_enrollment_row(session, event.id, request.user_id, for_update=True)
        if enrollment is None:
            raise ProblemError(ErrorCode.NOT_ENROLLED, "The requester is no longer enrolled")
        late = await late_core_open(session, event, formation, project, now)
        if enrollment.status == EnrollmentStatus.RESERVE.value and not late:
            raise ProblemError(ErrorCode.NOT_ENROLLED, "An active enrollment is required")
        if not late and now >= formation.formation_at:
            raise ProblemError(ErrorCode.INVALID_STATE, "Join requests close at the formation time")
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
        requester = await session.get(models.User, request.user_id)
        if requester is None:
            raise ProblemError(ErrorCode.NOT_FOUND)
        member = await session.scalar(
            select(models.ProjectMember.id).where(
                models.ProjectMember.project_id == project.id,
                models.ProjectMember.user_id == requester.id,
                models.ProjectMember.left_at.is_(None),
            )
        )
        if member is not None:
            raise ProblemError(
                ErrorCode.INVALID_STATE, "The requester is already a member of this project"
            )
        member_count = await session.scalar(live_count_query) or 0
        if member_count >= settings.project_size_max:
            raise ProblemError(
                ErrorCode.PROJECT_FULL, f"Project has reached max size {settings.project_size_max}"
            )
    request.status = JoinRequestStatus.ACCEPTED.value
    request.responded_at = now
    member_row = models.ProjectMember(
        project_id=project.id,
        event_id=event.id if event is not None else None,
        user_id=request.user_id,
        role=MemberRole.MEMBER.value,
        area_id=None,
        state=MemberState.CONFIRMED.value,
        trust=Trust.SELF.value,
        joined_via=JoinedVia.JOIN_REQUEST.value,
        proposal_reason=None,
        confirm_by=None,
        confirm_kind=None,
        is_team_row=event is not None,
        joined_at=now,
        confirmed_at=now,
    )
    session.add(member_row)
    reserve = False
    if enrollment is not None:
        reserve = enrollment.status == EnrollmentStatus.RESERVE.value
        enrollment.status = EnrollmentStatus.PLACED.value
        enrollment.status_at = now
        enrollment.reserve_rank = None
    await session.flush()
    if event is None:
        from hmm_core.projects import service as projects
        from hmm_core.recommendations import service as recommendations

        member_ids = await projects.project_member_ids(session, project.id)
        await notify_many(
            session,
            [user_id for user_id in member_ids if user_id != requester.id],
            ProjectMemberJoinedPayload(
                link=f"/projects/{project.id}",
                project_id=project.id,
                project_name=project.name,
                member=notification_person(requester),
                member_count=len(member_ids),
            ),
            now,
            dedupe_key=f"project_member_joined:{member_row.id}",
        )
        await recommendations.enqueue_project_refresh(session, jobs, project.id)
    if event is not None:
        await close_invitee_items(
            session,
            event,
            requester,
            now,
            closed_reason="joined_team",
            notified_reason="The invitee joined another team",
            keep=None,
        )
    project.last_lead_action_at = now
    await notify(
        session,
        requester.id,
        JoinRequestAcceptedPayload(
            link=f"/projects/{project.id}",
            project_id=project.id,
            project_name=project.name,
            join_request_id=request.id,
            requester=notification_person(requester),
            lead=notification_person(actor),
            event_id=event.id if event is not None else None,
            event_title=event.title if event is not None else None,
        ),
        now,
        dedupe_key=f"join_request_accepted:{request.id}",
    )
    if event is not None:
        await enqueue_participation_jobs(session, jobs, event.id, reserve=reserve)
    await session.flush()
    await session.refresh(request)
    return request


async def reject_join_request(
    session: AsyncSession, join_request_id: UUID, actor: models.User, now: datetime
) -> models.JoinRequest:
    request, project, event, formation = await join_request_context(session, join_request_id, actor)
    if project.lead_user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if request.status == JoinRequestStatus.REJECTED.value:
        return request
    if request.status != JoinRequestStatus.PENDING.value:
        raise ProblemError(ErrorCode.INVALID_STATE, "This join request is no longer pending")
    if formation is not None and FormationPhase(formation.phase) == FormationPhase.FORMING:
        raise ProblemError(ErrorCode.FORMATION_IN_PROGRESS, "Formation is running, retry shortly")
    request.status = JoinRequestStatus.REJECTED.value
    request.responded_at = now
    project.last_lead_action_at = now
    requester = await session.get(models.User, request.user_id)
    if requester is not None:
        await notify(
            session,
            requester.id,
            JoinRequestRejectedPayload(
                link=f"/projects/{project.id}",
                project_id=project.id,
                project_name=project.name,
                join_request_id=request.id,
                requester=notification_person(requester),
                lead=notification_person(actor),
                event_id=event.id if event is not None else None,
                event_title=event.title if event is not None else None,
            ),
            now,
            dedupe_key=f"join_request_rejected:{request.id}",
        )
    await session.flush()
    await session.refresh(request)
    return request


async def withdraw_user_pending_items(session: AsyncSession, user_id: UUID, now: datetime) -> None:
    await session.execute(
        update(models.Invite)
        .where(
            models.Invite.user_id == user_id,
            models.Invite.status == InviteStatus.PENDING.value,
        )
        .values(status=InviteStatus.WITHDRAWN.value, responded_at=now)
    )
    await session.execute(
        update(models.JoinRequest)
        .where(
            models.JoinRequest.user_id == user_id,
            models.JoinRequest.status == JoinRequestStatus.PENDING.value,
        )
        .values(status=JoinRequestStatus.WITHDRAWN.value, responded_at=now)
    )
    return None
