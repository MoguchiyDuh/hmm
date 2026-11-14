from datetime import datetime
from uuid import UUID

import procrastinate
from hmm_contracts.common import (
    ActorKind,
    AuditAction,
    AuditEntityType,
    ErrorCode,
    JoinedVia,
    MemberRole,
    MemberState,
    ProjectOrigin,
    ProjectStatus,
    RestrictionKind,
    TeamStatus,
    Trust,
)
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.platform.audit import record_audit
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.pagination import CursorParams, keyset_page
from hmm_core.platform.settings import load_settings
from hmm_core.projects import schemas
from hmm_core.projects.lifecycle import resolve_project_areas
from hmm_core.projects.views import CLOSED_PROJECT_STATUSES, project_response, project_views
from hmm_core.scores.restrictions import require_unrestricted


async def led_projects_count(session: AsyncSession, user_id: UUID) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(models.Project)
            .where(
                models.Project.lead_user_id == user_id,
                models.Project.status.notin_(sorted(CLOSED_PROJECT_STATUSES)),
            )
        )
        or 0
    )


async def get_project(
    session: AsyncSession, project_id: UUID, actor: models.User, now: datetime
) -> schemas.ProjectView:
    project = await session.get(models.Project, project_id)
    if project is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return (await project_views(session, [project], actor, now))[0]


async def list_projects(
    session: AsyncSession,
    actor: models.User,
    pagination: CursorParams,
    *,
    status: ProjectStatus | None,
    area: str | None,
    recruiting: bool | None,
    event_id: UUID | None,
    member_id: UUID | None,
    now: datetime,
) -> schemas.ProjectListViewPage:
    statement = select(models.Project)
    if status is not None:
        statement = statement.where(models.Project.status == status.value)
    if area is not None:
        statement = statement.where(
            models.Project.id.in_(
                select(models.ProjectArea.project_id)
                .join(models.Area, models.Area.id == models.ProjectArea.area_id)
                .where(models.Area.key == area)
            )
        )
    if recruiting is True:
        statement = statement.where(models.Project.recruiting.is_not(None))
    elif recruiting is False:
        statement = statement.where(models.Project.recruiting.is_(None))
    if event_id is not None:
        statement = statement.where(models.Project.event_id == event_id)
    if member_id is not None:
        statement = statement.where(
            models.Project.id.in_(
                select(models.ProjectMember.project_id).where(
                    models.ProjectMember.user_id == member_id,
                    models.ProjectMember.left_at.is_(None),
                )
            )
        )
    page = await keyset_page(
        session,
        statement,
        pagination,
        sort=[models.Project.created_at],
        id_column=models.Project.id,
        serialize=lambda row: row,
        descending=True,
    )
    return schemas.ProjectListViewPage(
        items=await project_views(session, page.items, actor, now),
        next_cursor=page.next_cursor,
    )


async def list_my_projects(
    session: AsyncSession,
    actor: models.User,
    status: ProjectStatus | TeamStatus | None,
    now: datetime,
) -> list[schemas.ProjectSummary]:
    statement = (
        select(models.Project)
        .join(models.ProjectMember, models.ProjectMember.project_id == models.Project.id)
        .where(models.ProjectMember.user_id == actor.id, models.ProjectMember.left_at.is_(None))
    )
    if status is not None:
        statement = statement.where(
            or_(models.Project.status == status.value, models.Project.team_status == status.value)
        )
    rows = list(
        await session.scalars(
            statement.order_by(models.Project.updated_at.desc(), models.Project.id.desc())
        )
    )
    views = await project_views(session, rows, actor, now)
    return [
        schemas.ProjectSummary.model_validate(view)
        for view in views
        if isinstance(view, schemas.Project)
    ]


async def create_project(
    session: AsyncSession,
    jobs: procrastinate.App,
    body: schemas.CreateProjectBody,
    actor: models.User,
    now: datetime,
) -> schemas.Project:
    from hmm_core.identity.service import lock_user
    from hmm_core.recommendations import service as recommendations

    await require_unrestricted(session, actor.id, RestrictionKind.CREATING_PROJECTS, now)
    if body.event_id is not None and await session.get(models.Event, body.event_id) is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    area_ids = await resolve_project_areas(session, body.area_keys)
    await lock_user(session, actor.id)
    if (
        await led_projects_count(session, actor.id)
        >= (await load_settings(session)).projects_led_max
    ):
        raise ProblemError(
            ErrorCode.PROJECTS_LED_LIMIT, "You already lead the maximum number of projects"
        )
    project = models.Project(
        event_id=body.event_id,
        name=body.name,
        description_md=body.description_md,
        goal=body.goal,
        links=[str(link) for link in body.links],
        visibility=body.visibility.value,
        recruiting=None,
        lead_user_id=actor.id,
        status=ProjectStatus.IDEA.value,
        team_status=None,
        origin=ProjectOrigin.STANDALONE.value,
        ready_at=None,
        review_ends_at=None,
        formed_in_run_id=None,
        locked_at=None,
        seat_open_since=None,
        seats_closed_at=None,
        last_lead_action_at=now,
        last_progress_at=now,
    )
    session.add(project)
    await session.flush()
    session.add_all(
        [models.ProjectArea(project_id=project.id, area_id=area_id) for area_id in area_ids]
    )
    session.add(
        models.ProjectMember(
            project_id=project.id,
            event_id=None,
            user_id=actor.id,
            role=MemberRole.LEAD.value,
            area_id=None,
            state=MemberState.CONFIRMED.value,
            trust=Trust.SELF.value,
            joined_via=JoinedVia.CREATE.value,
            proposal_reason=None,
            confirm_by=None,
            confirm_kind=None,
            is_team_row=False,
            joined_at=now,
            confirmed_at=now,
        )
    )
    await record_audit(
        session,
        actor=actor.id,
        actor_kind=ActorKind.USER,
        action=AuditAction.PROJECT_CREATE,
        entity_type=AuditEntityType.PROJECT,
        entity_id=project.id,
        before=None,
        after={
            "name": project.name,
            "origin": project.origin,
            "visibility": project.visibility,
            "lead_user_id": str(actor.id),
            "event_id": str(project.event_id) if project.event_id is not None else None,
        },
        reason=None,
    )
    await recommendations.enqueue_project_refresh(session, jobs, project.id)
    await session.flush()
    return await project_response(session, project.id, actor, now)
