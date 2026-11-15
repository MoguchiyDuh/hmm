from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm import models
from hmm.formation.common import (
    formation_row,
    formation_sizes,
    locked_formation_event,
    members,
    notification_person,
    queue_units,
    reg_deadline,
)
from hmm.formation.invites import expire_invites
from hmm.formation.teams import finalize_event, refill_event, remove_member
from hmm.notifications.payloads import (
    AcceptReminderPayload,
    ActionKey,
    AlertKind,
    ApiAction,
    FormationSoonPayload,
)
from hmm.notifications.service import notify, notify_many, raise_alert, resolve_alerts
from hmm.platform.jobs import JobName, Runtime, defer_job
from hmm.platform.settings import load_settings


async def remind_member(
    session: AsyncSession,
    project: models.Project,
    event: models.Event,
    row: models.ProjectMember,
    now: datetime,
) -> None:
    if row.accept_by is None:
        return None
    people = tuple(
        notification_person(user)
        for user in await session.scalars(
            select(models.User).where(
                models.User.id.in_(
                    [member.user_id for member in await members(session, project.id)]
                )
            )
        )
    )
    await notify(
        session,
        row.user_id,
        AcceptReminderPayload(
            link=f"/projects/{project.id}",
            event_id=event.id,
            event_title=event.title,
            project_id=project.id,
            team_name=project.name,
            project_member_id=row.id,
            accept_by=row.accept_by,
            timezone=event.timezone,
            members=people,
            hours_remaining=max(0, int((row.accept_by - now).total_seconds() // 3600)),
            actions=(
                ApiAction(
                    key=ActionKey.PLACEMENT_ACCEPT,
                    entity_id=project.id,
                    label_key="notifications.actions.accept",
                ),
                ApiAction(
                    key=ActionKey.PLACEMENT_DECLINE,
                    entity_id=project.id,
                    label_key="notifications.actions.decline",
                ),
            ),
        ),
        now,
        dedupe_key=f"accept_reminder:{row.id}",
    )


async def tick_event(
    session: AsyncSession, runtime: Runtime, event_id: UUID, now: datetime
) -> None:
    event = await locked_formation_event(session, event_id)
    formation = await formation_row(session, event_id)
    if event.status != "published" or formation.phase in {"forming", "closed"}:
        return None
    formation.updated_at = now
    await session.flush()
    await expire_invites(session, event, now)
    if formation.phase == "open":
        if now >= formation.formation_at:
            pending = await session.scalar(
                select(models.FormationRun.id).where(
                    models.FormationRun.event_id == event_id,
                    models.FormationRun.status == "pending",
                )
            )
            if pending is None:
                run = models.FormationRun(
                    event_id=event_id,
                    status="pending",
                    planner="algo",
                    input={},
                    validation_errors=[],
                    algo_version="",
                )
                session.add(run)
                await session.flush()
                await defer_job(
                    session,
                    runtime.jobs,
                    JobName.RUN_FORMATION,
                    event_id=str(event_id),
                    run_id=str(run.id),
                )
        if now >= formation.formation_at - timedelta(hours=24):
            units, grouped, _ = await queue_units(session, event_id)
            people = [row.user_id for group in grouped.values() for row in group]
            await notify_many(
                session,
                people,
                FormationSoonPayload(
                    link=f"/events/{event_id}/formation",
                    event_id=event_id,
                    event_title=event.title,
                    formation_at=formation.formation_at,
                    timezone=event.timezone,
                    enrolled_count=len(people),
                    team_count=len(units),
                ),
                now,
                dedupe_key=f"formation_soon:{event_id}",
            )
        return None
    for project in await session.scalars(
        select(models.Project).where(
            models.Project.event_id == event_id, models.Project.team_status == "formed"
        )
    ):
        for row in await members(session, project.id):
            if row.state != "proposed" or row.accept_by is None:
                continue
            if now >= row.accept_by:
                await remove_member(session, project, event, row, now, "lapsed", "lapsed")
            elif now >= row.accept_by - timedelta(hours=6):
                await remind_member(session, project, event, row, now)
    if now >= event.starts_at:
        await finalize_event(session, event, now)
    elif now < reg_deadline(event):
        await refill_event(session, event, now)
    if now >= reg_deadline(event):
        _, minimum, _ = formation_sizes(event, formation, await load_settings(session))
        for project in await session.scalars(
            select(models.Project).where(
                models.Project.event_id == event_id,
                models.Project.team_status.in_(["formed", "active"]),
            )
        ):
            if len(await members(session, project.id)) < minimum:
                await raise_alert(
                    session,
                    AlertKind.TEAM_BELOW_MIN,
                    now,
                    title="Team below minimum size",
                    message=f"Minimum team size is {minimum}",
                    link=f"/projects/{project.id}",
                    subject_type="project",
                    subject_id=project.id,
                    subject_title=project.name,
                    dedupe_key=f"team_below_min:{project.id}",
                )
            else:
                await resolve_alerts(session, f"team_below_min:{project.id}", now)
