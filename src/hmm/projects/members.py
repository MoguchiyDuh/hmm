from datetime import datetime
from uuid import UUID

import procrastinate
from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm import models
from hmm.common import (
    ActorKind,
    AuditAction,
    AuditEntityType,
    ErrorCode,
    JoinedVia,
    LeftReason,
    MemberState,
)
from hmm.formation import teams as formation_teams
from hmm.formation.schemas import DeclinePlacementBody
from hmm.identity import service as identity_service
from hmm.notifications.payloads import (
    ProjectMemberJoinedPayload,
    ProjectMemberRemovedPayload,
)
from hmm.notifications.service import notify, notify_many
from hmm.platform.audit import record_audit
from hmm.platform.db import get_or_404
from hmm.platform.errors import ProblemError
from hmm.platform.schemas import ReasonBody, notification_person
from hmm.platform.settings import load_settings
from hmm.projects import schemas, succession
from hmm.projects.common import project_member_ids
from hmm.projects.records import led_projects_count
from hmm.projects.views import (
    CLOSED_PROJECT_STATUSES,
    POST_RESULT_TEAM_STATUSES,
    project_response,
)
from hmm.recommendations import service as recommendations


async def remove_project_member(
    session: AsyncSession,
    jobs: procrastinate.App,
    project_id: UUID,
    user_id: UUID,
    body: ReasonBody | None,
    actor: models.User,
    now: datetime,
) -> None:

    project = await get_or_404(session, models.Project, project_id)
    if project.team_status is not None and project.team_status not in POST_RESULT_TEAM_STATUSES:
        await formation_teams.kick_member(session, project_id, user_id, actor, now)
        return
    project = await get_or_404(session, models.Project, project_id, lock=True)
    if project.status in CLOSED_PROJECT_STATUSES:
        raise ProblemError(ErrorCode.INVALID_STATE, "This project is closed")
    if project.lead_user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if user_id == actor.id:
        raise ProblemError(
            ErrorCode.INVALID_STATE, "You cannot remove yourself; leave the project instead"
        )
    member = await session.scalar(
        select(models.ProjectMember).where(
            models.ProjectMember.project_id == project.id,
            models.ProjectMember.user_id == user_id,
            models.ProjectMember.left_at.is_(None),
        )
    )
    if member is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    previous: dict[str, JsonValue] = {"role": member.role, "state": member.state}
    member.left_at = now
    member.left_reason = LeftReason.REMOVED.value
    await session.flush()
    await session.refresh(project)
    removed = await session.get(models.User, user_id)
    if removed is not None:
        await notify(
            session,
            removed.id,
            ProjectMemberRemovedPayload(
                link=f"/projects/{project.id}",
                project_id=project.id,
                project_name=project.name,
                member=notification_person(removed),
                actor=notification_person(actor),
                reason=body.reason if body is not None else None,
            ),
            now,
            dedupe_key=f"project_member_removed:{member.id}",
        )
    await record_audit(
        session,
        actor=actor.id,
        actor_kind=ActorKind.USER,
        action=AuditAction.MEMBER_REMOVE,
        entity_type=AuditEntityType.PROJECT_MEMBER,
        entity_id=member.id,
        before=previous,
        after={"left_reason": LeftReason.REMOVED.value},
        reason=body.reason if body is not None else None,
    )
    await recommendations.enqueue_project_refresh(session, jobs, project.id)
    await session.flush()


async def leave_project(
    session: AsyncSession,
    jobs: procrastinate.App,
    project_id: UUID,
    actor: models.User,
    now: datetime,
    body: DeclinePlacementBody | None = None,
) -> None:

    project = await get_or_404(session, models.Project, project_id)
    if project.team_status is not None and project.team_status not in POST_RESULT_TEAM_STATUSES:
        await formation_teams.leave_team(
            session, project_id, body or DeclinePlacementBody(), actor, now
        )
        return
    project = await get_or_404(session, models.Project, project_id, lock=True)
    member = await session.scalar(
        select(models.ProjectMember).where(
            models.ProjectMember.project_id == project.id,
            models.ProjectMember.user_id == actor.id,
            models.ProjectMember.left_at.is_(None),
        )
    )
    if member is None:
        raise ProblemError(ErrorCode.FORBIDDEN)
    await succession.depart_member(
        session,
        project,
        member,
        actor,
        LeftReason.LEFT,
        now,
        actor=actor.id,
        actor_kind=ActorKind.USER,
    )
    await recommendations.enqueue_project_refresh(session, jobs, project.id)
    await session.flush()


async def transfer_project_lead(
    session: AsyncSession,
    jobs: procrastinate.App,
    project_id: UUID,
    user_id: UUID,
    actor: models.User,
    now: datetime,
) -> schemas.Project:

    project = await get_or_404(session, models.Project, project_id)
    if project.team_status is not None and project.team_status not in POST_RESULT_TEAM_STATUSES:
        raise ProblemError(ErrorCode.INVALID_STATE)
    await identity_service.lock_user(session, user_id)
    project = await get_or_404(session, models.Project, project_id, lock=True)
    if project.team_status is not None and project.team_status not in POST_RESULT_TEAM_STATUSES:
        raise ProblemError(ErrorCode.INVALID_STATE)
    if project.lead_user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if project.status in CLOSED_PROJECT_STATUSES:
        raise ProblemError(ErrorCode.INVALID_STATE)
    if project.lead_user_id == user_id:
        return await project_response(session, project.id, actor)
    member = await session.scalar(
        select(models.ProjectMember).where(
            models.ProjectMember.project_id == project.id,
            models.ProjectMember.user_id == user_id,
            models.ProjectMember.state == MemberState.CONFIRMED.value,
            models.ProjectMember.left_at.is_(None),
        )
    )
    if member is None:
        raise ProblemError(ErrorCode.FORBIDDEN)
    user = await session.get(models.User, user_id)
    if user is None or user.banned_at is not None:
        raise ProblemError(ErrorCode.INVALID_STATE)
    if (
        await led_projects_count(session, user_id)
        >= (await load_settings(session)).projects_led_max
    ):
        raise ProblemError(
            ErrorCode.PROJECTS_LED_LIMIT, "You already lead the maximum number of projects"
        )
    await succession.assign_project_lead(session, project, member, now, actor)
    await recommendations.enqueue_project_refresh(session, jobs, project.id)
    await session.flush()
    await session.refresh(project)
    return await project_response(session, project.id, actor)


async def add_member(
    session: AsyncSession, project: models.Project, user_id: UUID, via: JoinedVia, now: datetime
) -> None:
    if project.status in CLOSED_PROJECT_STATUSES:
        raise ProblemError(ErrorCode.INVALID_STATE)
    ids = await project_member_ids(session, project.id)
    if user_id in ids:
        raise ProblemError(ErrorCode.INVALID_STATE)
    if len(ids) >= (await load_settings(session)).project_size_max:
        raise ProblemError(ErrorCode.PROJECT_FULL)
    row = models.ProjectMember(
        project_id=project.id,
        user_id=user_id,
        role="member",
        state="confirmed",
        trust="self",
        joined_via=via.value,
        is_team_row=project.team_status is not None,
        joined_at=now,
        confirmed_at=now,
    )
    session.add(row)
    await session.flush()
    await session.refresh(project)
    user = await session.get(models.User, user_id)
    if user is not None:
        await notify_many(
            session,
            ids,
            ProjectMemberJoinedPayload(
                link=f"/projects/{project.id}",
                project_id=project.id,
                project_name=project.name,
                member=notification_person(user),
                member_count=len(ids) + 1,
            ),
            now,
            dedupe_key=f"project_member_joined:{row.id}",
        )
