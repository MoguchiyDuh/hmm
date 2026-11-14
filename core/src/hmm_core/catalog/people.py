from datetime import datetime
from uuid import UUID

import procrastinate
from hmm_contracts.common import (
    AttendanceRole,
    AttendanceStatus,
    ConfirmationFactType,
    ConfirmationSource,
    EventStatus,
    PlaceStatus,
    ResultOutcome,
    SourcePersonRole,
    Trust,
)
from hmm_contracts.imports import EventImport, SourcePerson
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.feed import service as feed


async def match_person_user(session: AsyncSession, person: SourcePerson) -> models.User | None:
    if person.github_login:
        user = await session.scalar(
            select(models.User)
            .where(
                func.lower(models.User.github_login) == person.github_login.lower(),
                models.User.banned_at.is_(None),
            )
            .order_by(models.User.id)
            .limit(1)
        )
        if user is not None:
            return user
    if person.telegram_username:
        return await session.scalar(
            select(models.User)
            .join(models.TelegramLink, models.TelegramLink.user_id == models.User.id)
            .where(
                func.lower(models.TelegramLink.username) == person.telegram_username.lower(),
                models.TelegramLink.broken_at.is_(None),
                models.User.banned_at.is_(None),
            )
            .order_by(models.User.id)
            .limit(1)
        )
    return None


async def upsert_source_person(
    session: AsyncSession,
    event: models.Event,
    source_id: str,
    person: SourcePerson,
    matched_user_id: UUID | None,
) -> models.EventSourcePerson:
    identity = person.github_login or person.telegram_username or person.display_name
    row = await session.scalar(
        select(models.EventSourcePerson)
        .where(
            models.EventSourcePerson.source_id == source_id,
            func.coalesce(
                models.EventSourcePerson.github_login,
                models.EventSourcePerson.telegram_username,
                models.EventSourcePerson.display_name,
            )
            == identity,
        )
        .with_for_update()
    )
    if row is None:
        row = models.EventSourcePerson(
            event_id=event.id,
            source_id=source_id,
            github_login=person.github_login,
            telegram_username=person.telegram_username,
            display_name=person.display_name[:100],
            role=person.role.value,
            place=person.place,
            matched_user_id=matched_user_id,
        )
        session.add(row)
    else:
        row.display_name = person.display_name[:100]
        row.role = person.role.value
        row.place = person.place
        row.matched_user_id = matched_user_id or row.matched_user_id
    await session.flush()
    return row


async def apply_attendance_fact(
    session: AsyncSession,
    jobs: procrastinate.App,
    event: models.Event,
    person: SourcePerson,
    user: models.User,
    now: datetime,
) -> models.EventAttendance | None:
    role = (
        AttendanceRole.ATTENDEE
        if person.role == SourcePersonRole.WINNER
        else AttendanceRole(person.role.value)
    )
    attendance = await session.scalar(
        select(models.EventAttendance)
        .where(
            models.EventAttendance.event_id == event.id,
            models.EventAttendance.user_id == user.id,
        )
        .with_for_update()
    )
    if attendance is None:
        attendance = models.EventAttendance(
            event_id=event.id,
            user_id=user.id,
            status=AttendanceStatus.ATTENDED.value,
            role=role.value,
            proof_links=[],
            note=None,
            trust=Trust.SELF.value,
            status_at=now,
        )
        session.add(attendance)
    elif attendance.status == AttendanceStatus.CANCELLED:
        return None
    else:
        if attendance.status == AttendanceStatus.GOING:
            attendance.status = AttendanceStatus.ATTENDED.value
            attendance.status_at = now
        if attendance.role == AttendanceRole.ATTENDEE and role != AttendanceRole.ATTENDEE:
            attendance.role = role.value
    await session.flush()
    await feed.add_confirmation(
        session,
        jobs,
        fact_type=ConfirmationFactType.ATTENDANCE,
        fact_id=attendance.id,
        subject_user_id=user.id,
        confirmer_id=None,
        source=ConfirmationSource.SYSTEM,
        now=now,
    )
    return attendance


async def team_project_id(session: AsyncSession, event_id: UUID, user_id: UUID) -> UUID | None:
    return await session.scalar(
        select(models.ProjectMember.project_id)
        .where(
            models.ProjectMember.event_id == event_id,
            models.ProjectMember.user_id == user_id,
            models.ProjectMember.left_at.is_(None),
            models.ProjectMember.is_team_row,
        )
        .order_by(models.ProjectMember.joined_at.desc(), models.ProjectMember.id.desc())
        .limit(1)
    )


async def apply_winner_places(
    session: AsyncSession, places: dict[UUID, int], now: datetime
) -> None:
    for project_id, place in places.items():
        result = await session.scalar(
            select(models.ProjectResult)
            .where(models.ProjectResult.project_id == project_id)
            .with_for_update()
        )
        if (
            result is None
            or result.outcome != ResultOutcome.FINISHED.value
            or result.place_status == PlaceStatus.REJECTED.value
        ):
            continue
        result.place = place
        result.place_status = PlaceStatus.VERIFIED.value
        result.place_verified_by = None
        result.place_verified_at = now


async def sync_people(
    session: AsyncSession,
    jobs: procrastinate.App,
    event: models.Event,
    item: EventImport,
    now: datetime,
) -> None:
    if event.status in (EventStatus.CANCELLED.value, EventStatus.ARCHIVED.value):
        return
    places: dict[UUID, int] = {}
    for person in item.people:
        user = await match_person_user(session, person)
        await upsert_source_person(
            session, event, item.source.source_id, person, user.id if user else None
        )
        if user is None:
            continue
        await apply_attendance_fact(session, jobs, event, person, user, now)
        if person.role == SourcePersonRole.WINNER and person.place is not None:
            project_id = await team_project_id(session, event.id, user.id)
            if project_id is not None:
                places[project_id] = min(places.get(project_id, person.place), person.place)
    await apply_winner_places(session, places, now)
