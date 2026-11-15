from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm import models
from hmm.common import ErrorCode
from hmm.formation import schemas
from hmm.formation.common import (
    enrollment_view,
    formation_sizes,
    live_enrollment_row,
    participation_event,
    queue_open,
)
from hmm.formation.queue import queue_user
from hmm.platform.errors import ProblemError
from hmm.platform.settings import load_settings


async def party_rows(session: AsyncSession, party_id: UUID) -> list[models.Enrollment]:
    return list(
        await session.scalars(
            select(models.Enrollment)
            .where(models.Enrollment.party_id == party_id)
            .order_by(models.Enrollment.status_at, models.Enrollment.id)
        )
    )


async def party_view(session: AsyncSession, party: models.Party) -> schemas.Party:
    await session.flush()
    await session.refresh(party)
    invites = await session.scalars(
        select(models.Invite)
        .where(models.Invite.party_id == party.id, models.Invite.status == "pending")
        .order_by(models.Invite.created_at, models.Invite.id)
    )
    rows = await party_rows(session, party.id)
    for row in rows:
        await session.refresh(row)
    return schemas.Party(
        id=party.id,
        event_id=party.event_id,
        owner_user_id=party.owner_user_id,
        closed=party.closed,
        created_at=party.created_at,
        members=[enrollment_view(row) for row in rows],
        invites=[schemas.Invite.model_validate(row, from_attributes=True) for row in invites],
    )


async def get_party(session: AsyncSession, party_id: UUID, actor: models.User) -> schemas.Party:
    party = await session.get(models.Party, party_id)
    if party is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    rows = await party_rows(session, party_id)
    if not actor.is_admin and actor.id not in {row.user_id for row in rows}:
        raise ProblemError(ErrorCode.FORBIDDEN)
    return await party_view(session, party)


async def locked_party(
    session: AsyncSession, party_id: UUID, actor: models.User, now: datetime
) -> tuple[models.Party, models.Event, models.EventFormation]:
    party = await session.get(models.Party, party_id)
    if party is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    event, formation = await participation_event(session, party.event_id, actor)
    await session.refresh(party)
    queue_open(event, formation, now)
    return party, event, formation


async def create_party(
    session: AsyncSession, event_id: UUID, actor: models.User, now: datetime
) -> schemas.Party:
    event, formation = await participation_event(session, event_id, actor)
    queue_open(event, formation, now)
    row = await queue_user(session, event, formation, actor.id, now)
    if row.party_id is not None:
        party = await session.get(models.Party, row.party_id)
        if party is None:
            raise ProblemError(ErrorCode.NOT_FOUND)
        return await party_view(session, party)
    party = models.Party(event_id=event_id, owner_user_id=actor.id, closed=False, created_at=now)
    session.add(party)
    await session.flush()
    row.party_id = party.id
    row.status_at = now
    await session.flush()
    await session.refresh(row)
    return await party_view(session, party)


async def update_party(
    session: AsyncSession,
    party_id: UUID,
    body: schemas.ClosedBody,
    actor: models.User,
    now: datetime,
) -> schemas.Party:
    party, event, formation = await locked_party(session, party_id, actor, now)
    if actor.id != party.owner_user_id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    _, minimum, _ = formation_sizes(event, formation, await load_settings(session))
    if body.closed and len(await party_rows(session, party_id)) < minimum:
        raise ProblemError(ErrorCode.PARTY_TOO_SMALL)
    party.closed = body.closed
    return await party_view(session, party)


async def detach_party(session: AsyncSession, row: models.Enrollment, now: datetime) -> None:
    if row.party_id is None:
        return None
    party = await session.get(models.Party, row.party_id)
    row.party_id = None
    row.status_at = now
    await session.flush()
    if party is None:
        return None
    remaining = await party_rows(session, party.id)
    if not remaining:
        await session.delete(party)
    else:
        if party.owner_user_id == row.user_id:
            party.owner_user_id = remaining[0].user_id
        event = await session.get(models.Event, party.event_id)
        formation = await session.get(models.EventFormation, party.event_id)
        if event is not None and formation is not None:
            _, minimum, _ = formation_sizes(event, formation, await load_settings(session))
            if len(remaining) < minimum:
                party.closed = False
    await session.flush()


async def leave_party(
    session: AsyncSession, party_id: UUID, actor: models.User, now: datetime
) -> None:
    party, _, _ = await locked_party(session, party_id, actor, now)
    row = await live_enrollment_row(session, party.event_id, actor.id)
    if row is None or row.party_id != party_id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    await detach_party(session, row, now)


async def kick_party_member(
    session: AsyncSession, party_id: UUID, user_id: UUID, actor: models.User, now: datetime
) -> None:
    party, _, _ = await locked_party(session, party_id, actor, now)
    if party.owner_user_id != actor.id or user_id == actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    row = await live_enrollment_row(session, party.event_id, user_id)
    if row is None or row.party_id != party_id:
        raise ProblemError(ErrorCode.NOT_FOUND)
    await detach_party(session, row, now)
