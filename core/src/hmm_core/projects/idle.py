from datetime import datetime, timedelta

from hmm_contracts.common import ActorKind, AuditAction, AuditEntityType, ProjectStatus
from hmm_contracts.notifications import ProjectIdlePayload, ProjectPausedPayload
from sqlalchemy import BigInteger, Text, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.notifications.service import notify, notify_many
from hmm_core.platform.audit import record_audit
from hmm_core.platform.settings import load_settings
from hmm_core.projects.common import project_member_ids
from hmm_core.projects.views import POST_RESULT_TEAM_STATUSES, lifecycle_open

PAUSE_MANUAL = "manual"
PAUSE_IDLE = "idle"
IDLE_PAUSE_FACTOR = 2


def resume_idle_project(project: models.Project) -> bool:
    if project.status == ProjectStatus.PAUSED.value and project.paused_reason == PAUSE_IDLE:
        project.status = ProjectStatus.ACTIVE.value
        project.paused_reason = None
        return True
    return False


def mark_project_progress(
    project: models.Project, now: datetime, *, start_idea: bool = True
) -> None:
    project.last_progress_at = now
    if not lifecycle_open(project):
        return None
    if start_idea and project.status == ProjectStatus.IDEA.value:
        project.status = ProjectStatus.ACTIVE.value
        project.paused_reason = None
        return None
    resume_idle_project(project)


async def warn_idle_projects(session: AsyncSession, now: datetime, *, limit: int = 50) -> int:
    idle_days = (await load_settings(session)).project_idle_days
    threshold = now - timedelta(days=idle_days)
    dedupe_key = func.concat(
        "project_idle:",
        cast(models.Project.id, Text),
        ":",
        cast(
            cast(func.trunc(func.extract("epoch", models.Project.last_progress_at)), BigInteger),
            Text,
        ),
    )
    projects = await session.scalars(
        select(models.Project)
        .where(
            models.Project.status.in_([ProjectStatus.IDEA.value, ProjectStatus.ACTIVE.value]),
            models.Project.lead_user_id.is_not(None),
            models.Project.last_progress_at.is_not(None),
            models.Project.last_progress_at <= threshold,
            or_(
                models.Project.team_status.is_(None),
                models.Project.team_status.in_(sorted(POST_RESULT_TEAM_STATUSES)),
            ),
            ~select(models.Notification.id)
            .where(models.Notification.dedupe_key == dedupe_key)
            .exists(),
        )
        .order_by(models.Project.last_progress_at, models.Project.id)
        .limit(limit)
    )
    count = 0
    for project in projects:
        if project.lead_user_id is None or project.last_progress_at is None:
            continue
        notification = await notify(
            session,
            project.lead_user_id,
            ProjectIdlePayload(
                link=f"/projects/{project.id}",
                project_id=project.id,
                project_name=project.name,
                idle_days=(now - project.last_progress_at).days,
                pauses_at=project.last_progress_at + timedelta(days=IDLE_PAUSE_FACTOR * idle_days),
            ),
            now,
            dedupe_key=f"project_idle:{project.id}:{int(project.last_progress_at.timestamp())}",
        )
        count += notification is not None
    return count


async def pause_idle_projects(session: AsyncSession, now: datetime, *, limit: int = 50) -> int:
    from hmm_core.feed import service as feed

    idle_days = (await load_settings(session)).project_idle_days
    threshold = now - timedelta(days=IDLE_PAUSE_FACTOR * idle_days)
    projects = await session.scalars(
        select(models.Project)
        .where(
            models.Project.status.in_([ProjectStatus.IDEA.value, ProjectStatus.ACTIVE.value]),
            models.Project.lead_user_id.is_not(None),
            models.Project.last_progress_at.is_not(None),
            models.Project.last_progress_at <= threshold,
            or_(
                models.Project.team_status.is_(None),
                models.Project.team_status.in_(sorted(POST_RESULT_TEAM_STATUSES)),
            ),
        )
        .order_by(models.Project.last_progress_at, models.Project.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    count = 0
    for project in projects:
        if project.last_progress_at is None:
            continue
        previous = project.status
        project.status = ProjectStatus.PAUSED.value
        project.paused_reason = PAUSE_IDLE
        await session.flush()
        await record_audit(
            session,
            actor=None,
            actor_kind=ActorKind.SYSTEM,
            action=AuditAction.SYSTEM_TRANSITION,
            entity_type=AuditEntityType.PROJECT,
            entity_id=project.id,
            before={"status": previous},
            after={"status": ProjectStatus.PAUSED.value, "paused_reason": PAUSE_IDLE},
            reason="Project inactive",
        )
        await feed.hide_feed_subject(session, "project", project.id, now)
        member_ids = await project_member_ids(session, project.id)
        if member_ids:
            await notify_many(
                session,
                member_ids,
                ProjectPausedPayload(
                    link=f"/projects/{project.id}",
                    project_id=project.id,
                    project_name=project.name,
                    status=ProjectStatus.PAUSED,
                    reason=f"No progress for {IDLE_PAUSE_FACTOR * idle_days} days",
                ),
                now,
                dedupe_key=f"project_idle_paused:{project.id}:{int(project.last_progress_at.timestamp())}",
            )
        count += 1
    return count


async def run_project_timers(session: AsyncSession, now: datetime, *, limit: int = 50) -> None:
    await warn_idle_projects(session, now, limit=limit)
    await pause_idle_projects(session, now, limit=limit)
