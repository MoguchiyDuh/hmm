from datetime import datetime
from uuid import UUID

import procrastinate
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm import models
from hmm.common import ErrorCode, JoinedVia
from hmm.notifications.payloads import (
    ActionKey,
    ApiAction,
    JoinRequestAcceptedPayload,
    JoinRequestRejectedPayload,
    ProjectJoinRequestReceivedPayload,
)
from hmm.notifications.service import notify
from hmm.platform.db import get_or_404
from hmm.platform.errors import ProblemError
from hmm.platform.schemas import notification_person
from hmm.projects import schemas
from hmm.projects.common import project_member_ids
from hmm.projects.invites import add_member, eligible_invitee
from hmm.projects.views import CLOSED_PROJECT_STATUSES, join_request_view


async def request_view(
    session: AsyncSession, row: models.JoinRequest, project: models.Project, actor: models.User
) -> schemas.JoinRequest:
    await session.flush()
    await session.refresh(row)
    user = await get_or_404(session, models.User, row.user_id)
    return join_request_view(project, row, users={user.id: user}, actor=actor)


async def create_join_request(
    session: AsyncSession,
    project_id: UUID,
    body: schemas.CreateProjectJoinRequestBody,
    actor: models.User,
    now: datetime,
) -> schemas.JoinRequest:
    project = await get_or_404(session, models.Project, project_id, lock=True)
    if (
        project.team_status is not None
        or project.status in CLOSED_PROJECT_STATUSES
        or project.recruiting is None
        or project.lead_user_id is None
    ):
        raise ProblemError(ErrorCode.INVALID_STATE)
    if actor.id in await project_member_ids(session, project.id):
        raise ProblemError(ErrorCode.INVALID_STATE)
    if (
        await session.scalar(
            select(models.JoinRequest.id).where(
                models.JoinRequest.project_id == project.id,
                models.JoinRequest.user_id == actor.id,
                models.JoinRequest.status == "pending",
            )
        )
        is not None
    ):
        raise ProblemError(ErrorCode.JOIN_REQUEST_PENDING)
    row = models.JoinRequest(
        project_id=project.id, user_id=actor.id, note=body.note, status="pending"
    )
    session.add(row)
    await session.flush()
    await session.refresh(project)
    await notify(
        session,
        project.lead_user_id,
        ProjectJoinRequestReceivedPayload(
            link=f"/projects/{project.id}",
            project_id=project.id,
            project_name=project.name,
            join_request_id=row.id,
            requester=notification_person(actor),
            note=body.note,
            actions=(
                ApiAction(key=ActionKey.JOIN_REQUEST_ACCEPT, entity_id=row.id, label_key="accept"),
                ApiAction(key=ActionKey.JOIN_REQUEST_REJECT, entity_id=row.id, label_key="reject"),
            ),
        ),
        now,
        dedupe_key=f"project_join_request_received:{row.id}",
    )
    return await request_view(session, row, project, actor)


async def list_join_requests(
    session: AsyncSession, project_id: UUID, actor: models.User
) -> list[schemas.JoinRequest]:
    project = await get_or_404(session, models.Project, project_id, lock=True)
    if project.team_status is not None:
        raise ProblemError(ErrorCode.INVALID_STATE)
    if project.lead_user_id != actor.id and not actor.is_admin:
        raise ProblemError(ErrorCode.FORBIDDEN)
    rows = list(
        await session.scalars(
            select(models.JoinRequest)
            .where(models.JoinRequest.project_id == project.id)
            .order_by(models.JoinRequest.created_at.desc(), models.JoinRequest.id.desc())
        )
    )
    return [await request_view(session, row, project, actor) for row in rows]


async def respond_join_request(
    session: AsyncSession,
    jobs: procrastinate.App,
    request_id: UUID,
    actor: models.User,
    now: datetime,
    action: str,
) -> schemas.JoinRequest:
    from hmm.recommendations.service import enqueue_project_refresh

    row = await get_or_404(session, models.JoinRequest, request_id)
    project = await get_or_404(session, models.Project, row.project_id, lock=True)
    await session.refresh(row)
    if project.team_status is not None:
        raise ProblemError(ErrorCode.INVALID_STATE)
    if (action == "withdrawn" and row.user_id != actor.id) or (
        action != "withdrawn" and project.lead_user_id != actor.id
    ):
        raise ProblemError(ErrorCode.FORBIDDEN)
    if row.status == action:
        return await request_view(session, row, project, actor)
    if row.status != "pending":
        raise ProblemError(ErrorCode.INVALID_STATE)
    if action == "accepted":
        await eligible_invitee(session, row.user_id, now)
        await add_member(session, project, row.user_id, JoinedVia.JOIN_REQUEST, now)
        await enqueue_project_refresh(session, jobs, project.id)
    row.status = action
    row.responded_at = now
    user = await session.get(models.User, row.user_id)
    if action != "withdrawn" and user is not None:
        payload = JoinRequestAcceptedPayload if action == "accepted" else JoinRequestRejectedPayload
        await notify(
            session,
            user.id,
            payload(
                link=f"/projects/{project.id}",
                project_id=project.id,
                project_name=project.name,
                join_request_id=row.id,
                requester=notification_person(user),
                lead=notification_person(actor),
            ),
            now,
            dedupe_key=f"join_request_{action}:{row.id}",
        )
    return await request_view(session, row, project, actor)
