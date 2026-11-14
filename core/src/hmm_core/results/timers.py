from datetime import datetime, timedelta

import procrastinate
from hmm_contracts.common import (
    ActorKind,
    AuditAction,
    AuditEntityType,
    ProjectStatus,
    ResultOutcome,
    TeamStatus,
)
from hmm_contracts.notifications import AlertKind, ResultDuePayload
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.notifications.service import notify, raise_alert, resolve_alerts
from hmm_core.platform.audit import record_audit
from hmm_core.platform.jobs import JobName, defer_job
from hmm_core.platform.settings import load_settings
from hmm_core.results.submissions import team_member_ids


def result_due_payload(
    project: models.Project,
    event: models.Event,
    *,
    member_submit_at: datetime,
    closes_at: datetime,
) -> ResultDuePayload:
    return ResultDuePayload(
        link=f"/projects/{project.id}",
        actions=(),
        event_id=event.id,
        event_title=event.title,
        project_id=project.id,
        team_name=project.name,
        ends_at=event.ends_at,
        member_submit_at=member_submit_at,
        closes_at=closes_at,
    )


async def _active_teams(session: AsyncSession) -> list[models.Project]:
    return list(
        await session.scalars(
            select(models.Project)
            .where(models.Project.team_status == TeamStatus.ACTIVE.value)
            .order_by(models.Project.id)
        )
    )


async def send_result_due(session: AsyncSession, now: datetime, *, limit: int = 500) -> int:
    settings = await load_settings(session)
    member_after = timedelta(days=settings.results_due_days)
    close_after = timedelta(days=settings.results_close_days)
    created = 0
    processed = 0
    for project in await _active_teams(session):
        if processed >= limit:
            break
        event = await session.get(models.Event, project.event_id) if project.event_id else None
        if event is None or now < event.ends_at:
            continue
        if await session.get(models.ProjectResult, project.id) is not None:
            continue
        member_submit_at = event.ends_at + member_after
        payload = result_due_payload(
            project,
            event,
            member_submit_at=member_submit_at,
            closes_at=event.ends_at + close_after,
        )
        sent = 0
        if project.lead_user_id is not None and (
            await notify(
                session,
                project.lead_user_id,
                payload,
                now,
                dedupe_key=f"result_due:{project.id}:{project.lead_user_id}",
            )
            is not None
        ):
            sent += 1
        if now >= member_submit_at:
            team_ids = await team_member_ids(session, project.id)
            for user_id in team_ids:
                if (
                    await notify(
                        session,
                        user_id,
                        payload,
                        now,
                        dedupe_key=f"result_due:{project.id}:{user_id}",
                    )
                    is not None
                ):
                    sent += 1
            await raise_alert(
                session,
                AlertKind.RESULT_OVERDUE,
                now,
                title="Result overdue",
                message=f"No result was submitted for {event.title}.",
                link=f"/projects/{project.id}",
                subject_type="project",
                subject_id=project.id,
                subject_title=project.name,
                dedupe_key=f"result_overdue:{project.id}",
            )
            bucket = int((now - member_submit_at).total_seconds() // (3 * 86400))
            if bucket >= 1:
                for user_id in team_ids:
                    if (
                        await notify(
                            session,
                            user_id,
                            payload,
                            now,
                            dedupe_key=f"result_due_reminder:{project.id}:{user_id}:{bucket}",
                        )
                        is not None
                    ):
                        sent += 1
        if sent:
            created += sent
            processed += 1
    return created


async def auto_close_result_teams(
    session: AsyncSession, jobs: procrastinate.App, now: datetime, *, limit: int = 500
) -> int:
    settings = await load_settings(session)
    close_after = timedelta(days=settings.results_close_days)
    closed = 0
    for project in await _active_teams(session):
        if closed >= limit:
            break
        event = await session.get(models.Event, project.event_id) if project.event_id else None
        if event is None or now < event.ends_at + close_after:
            continue
        locked = await session.scalar(
            select(models.Project)
            .where(models.Project.id == project.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            locked is None
            or locked.team_status != TeamStatus.ACTIVE.value
            or await session.get(models.ProjectResult, project.id) is not None
        ):
            continue
        session.add(
            models.ProjectResult(
                project_id=project.id,
                outcome=ResultOutcome.WITHDRAWN.value,
                place=None,
                award=None,
                place_status=None,
                place_verified_by=None,
                place_verified_at=None,
                repo_url=None,
                demo_url=None,
                presentation_url=None,
                retrospective_md=None,
                member_areas={},
                participants={},
                auto_closed=True,
                submitted_by=None,
                submitted_at=None,
                edited_at=None,
            )
        )
        locked.team_status = TeamStatus.WITHDRAWN.value
        locked.status = ProjectStatus.ABANDONED.value
        await session.flush()
        for user_id in await team_member_ids(session, project.id):
            await defer_job(session, jobs, JobName.RECOMPUTE_MEMBER, user_id=str(user_id))
        await resolve_alerts(
            session, f"result_overdue:{project.id}", now, note="Auto-closed without a result"
        )
        await record_audit(
            session,
            actor=None,
            actor_kind=ActorKind.SYSTEM,
            action=AuditAction.SYSTEM_TRANSITION,
            entity_type=AuditEntityType.PROJECT_RESULT,
            entity_id=project.id,
            after={"outcome": ResultOutcome.WITHDRAWN.value, "auto_closed": True},
        )
        closed += 1
    return closed


async def run_result_timers(session: AsyncSession, jobs: procrastinate.App, now: datetime) -> None:
    await send_result_due(session, now)
    await auto_close_result_teams(session, jobs, now)
