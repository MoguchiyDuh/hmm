from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm import models
from hmm.algo.pack import pack
from hmm.common import ErrorCode, FormationPhase
from hmm.formation import schemas
from hmm.formation.common import (
    area_id,
    eligible_user,
    enrollment_view,
    formation_event,
    formation_sizes,
    live_enrollment_row,
    participation_event,
    queue_open,
    queue_units,
    set_enrollment_status,
    team_member,
    team_view,
)
from hmm.platform.errors import ProblemError
from hmm.platform.pagination import CursorParams, keyset_page
from hmm.platform.settings import load_settings


async def queue_user(
    session: AsyncSession,
    event: models.Event,
    formation: models.EventFormation,
    user_id: UUID,
    now: datetime,
    source: str = "self",
) -> models.Enrollment:
    await eligible_user(session, user_id, event.id, now)
    row = await live_enrollment_row(session, event.id, user_id)
    if row is not None:
        return row
    row = await session.scalar(
        select(models.Enrollment)
        .where(models.Enrollment.event_id == event.id, models.Enrollment.user_id == user_id)
        .order_by(models.Enrollment.status_at.desc(), models.Enrollment.id.desc())
        .limit(1)
    )
    if row is None:
        row = models.Enrollment(
            event_id=event.id, user_id=user_id, source=source, willing_to_lead=False
        )
        session.add(row)
    row.party_id = None
    row.status = "active" if formation.phase == "open" else "reserve"
    row.status_at = now
    await session.flush()
    await session.refresh(row)
    return row


async def enroll(
    session: AsyncSession,
    event_id: UUID,
    body: schemas.CreateEnrollmentBody,
    actor: models.User,
    now: datetime,
) -> schemas.Enrollment:
    event, formation = await participation_event(session, event_id)
    queue_open(event, formation, now)
    await eligible_user(session, actor.id, event_id, now)
    if await live_enrollment_row(session, event_id, actor.id) is not None:
        raise ProblemError(ErrorCode.ALREADY_ENROLLED)
    row = await queue_user(session, event, formation, actor.id, now)
    row.preferred_area_id = await area_id(session, body.preferred_area_key)
    row.note = body.note
    row.willing_to_lead = body.willing_to_lead
    await session.flush()
    await session.refresh(row)
    return enrollment_view(row)


async def update_enrollment(
    session: AsyncSession,
    event_id: UUID,
    body: schemas.UpdateEnrollmentBody,
    actor: models.User,
    now: datetime,
) -> schemas.Enrollment:
    event, formation = await participation_event(session, event_id)
    queue_open(event, formation, now)
    row = await live_enrollment_row(session, event_id, actor.id)
    if row is None:
        raise ProblemError(ErrorCode.NOT_ENROLLED)
    if "preferred_area_key" in body.model_fields_set:
        row.preferred_area_id = await area_id(session, body.preferred_area_key)
    if "note" in body.model_fields_set:
        row.note = body.note
    if body.willing_to_lead is not None:
        row.willing_to_lead = body.willing_to_lead
    await session.flush()
    await session.refresh(row)
    return enrollment_view(row)


async def withdraw_enrollment(
    session: AsyncSession, event_id: UUID, actor: models.User, now: datetime
) -> None:
    from hmm.formation.parties import detach_party

    event, _ = await participation_event(session, event_id)
    if now >= event.starts_at:
        raise ProblemError(ErrorCode.ENROLLMENT_CLOSED)
    if await team_member(session, event_id, actor.id) is not None:
        raise ProblemError(ErrorCode.ALREADY_IN_TEAM)
    row = await live_enrollment_row(session, event_id, actor.id)
    if row is None:
        previous = await session.scalar(
            select(models.Enrollment.id).where(
                models.Enrollment.event_id == event_id,
                models.Enrollment.user_id == actor.id,
                models.Enrollment.status == "withdrawn",
            )
        )
        if previous is not None:
            return None
        raise ProblemError(ErrorCode.NOT_ENROLLED)
    await detach_party(session, row, now)
    await set_enrollment_status(session, row, "withdrawn", now)


async def list_enrollments(
    session: AsyncSession, event_id: UUID, actor: models.User, params: CursorParams
) -> schemas.EnrollmentPage:
    await formation_event(session, event_id, actor)
    page = await keyset_page(
        session,
        select(models.Enrollment).where(
            models.Enrollment.event_id == event_id,
            models.Enrollment.status.in_(["active", "reserve", "placed"]),
        ),
        params,
        sort=[models.Enrollment.status_at],
        id_column=models.Enrollment.id,
        serialize=enrollment_view,
        descending=False,
    )
    teams = [
        await team_view(session, team)
        for team in await session.scalars(
            select(models.Project)
            .where(models.Project.event_id == event_id, models.Project.team_status.is_not(None))
            .order_by(models.Project.created_at, models.Project.id)
        )
    ]
    return schemas.EnrollmentPage(items=page.items, next_cursor=page.next_cursor, teams=teams)


async def my_enrollments(
    session: AsyncSession, actor: models.User, status: str | None = None
) -> list[schemas.Enrollment]:
    query = select(models.Enrollment).where(models.Enrollment.user_id == actor.id)
    if status is not None:
        query = query.where(models.Enrollment.status == status)
    return [
        enrollment_view(row)
        for row in await session.scalars(
            query.order_by(models.Enrollment.status_at.desc(), models.Enrollment.id.desc())
        )
    ]


async def formation_participation(
    session: AsyncSession, event: models.Event, actor: models.User, now: datetime
) -> schemas.Formation | None:
    if not event.formation_enabled:
        return None
    formation = await session.scalar(
        select(models.EventFormation).where(models.EventFormation.event_id == event.id)
    )
    if formation is None:
        return None
    target, minimum, maximum = formation_sizes(event, formation, await load_settings(session))
    units, grouped, parties = await queue_units(session, event.id)
    from hmm.formation.teams import open_teams

    teams = (
        await open_teams(session, event.id, target, grouped) if formation.phase == "formed" else []
    )
    projection = pack(units, teams, target, minimum, maximum)
    projection = projection.model_copy(
        update={
            "new_teams": [
                *[
                    [party.id]
                    for party in parties
                    if party.closed and len(grouped.get(party.id, [])) >= minimum
                ],
                *projection.new_teams,
            ]
        }
    )
    row = await session.scalar(
        select(models.Enrollment)
        .where(models.Enrollment.event_id == event.id, models.Enrollment.user_id == actor.id)
        .order_by(models.Enrollment.status_at.desc(), models.Enrollment.id.desc())
        .limit(1)
    )
    member = await team_member(session, event.id, actor.id)
    project = await session.get(models.Project, member.project_id) if member else None
    actions: list[schemas.FormationAction] = []
    if formation.phase != "forming":
        if project is not None:
            actions.append(schemas.FormationAction.LEAVE)
            if member is not None and member.state == "proposed":
                actions.extend([schemas.FormationAction.ACCEPT, schemas.FormationAction.DECLINE])
        elif now < event.starts_at and row is not None and row.status in {"active", "reserve"}:
            actions.append(schemas.FormationAction.WITHDRAW)
        if (
            event.status == "published"
            and formation.phase in {"open", "formed"}
            and now < (event.registration_deadline or event.starts_at)
            and project is None
        ):
            actions.extend(
                [
                    schemas.FormationAction.UPDATE_ENROLLMENT,
                    schemas.FormationAction.CREATE_PARTY,
                ]
                if row is not None and row.status in {"active", "reserve"}
                else [schemas.FormationAction.ENROLL, schemas.FormationAction.CREATE_PARTY]
            )
    team_count = await session.scalar(
        select(func.count())
        .select_from(models.Project)
        .where(
            models.Project.event_id == event.id,
            models.Project.team_status.in_(["formed", "active"]),
        )
    )
    return schemas.Formation(
        phase=FormationPhase(formation.phase),
        target=target,
        min=minimum,
        max=maximum,
        formation_at=formation.formation_at,
        counts={
            "queued": sum(len(group) for group in grouped.values()),
            "parties": len(parties),
            "teams": team_count or 0,
        },
        projection=projection,
        my_enrollment=enrollment_view(row) if row else None,
        my_team=await team_view(session, project) if project else None,
        allowed_actions=actions,
    )
