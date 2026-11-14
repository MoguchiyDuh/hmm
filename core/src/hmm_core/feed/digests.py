from datetime import datetime, timedelta
from uuid import UUID

from hmm_contracts.notifications import (
    AttendanceConfirmationCandidate,
    AttendanceConfirmDigestPayload,
    ContributionsConfirmDigestPayload,
    NotificationPerson,
    NotificationType,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.notifications.service import notify
from hmm_core.platform.settings import load_settings


async def send_attendance_digest(session: AsyncSession, event_id: UUID, now: datetime) -> int:
    event = await session.get(models.Event, event_id)
    if event is None or event.status not in ("published", "archived") or event.ends_at > now:
        return 0
    attended = (
        await session.execute(
            select(models.EventAttendance, models.User.display_name)
            .join(models.User)
            .where(
                models.EventAttendance.event_id == event.id,
                models.EventAttendance.status == "attended",
                models.User.banned_at.is_(None),
            )
            .order_by(models.User.display_name, models.EventAttendance.id)
        )
    ).all()
    if not attended:
        return 0
    recipients = dict.fromkeys(row.user_id for row, _ in attended)
    if event.origin == "member" and event.created_by is not None:
        creator = await session.get(models.User, event.created_by)
        if creator is not None and creator.banned_at is None:
            recipients[creator.id] = None
    created = 0
    for recipient in recipients:
        candidates = tuple(
            AttendanceConfirmationCandidate(
                user_id=row.user_id, display_name=name, attendance_id=row.id
            )
            for row, name in attended
            if row.user_id != recipient
        )
        if not candidates:
            continue
        payload = AttendanceConfirmDigestPayload(
            type=NotificationType.ATTENDANCE_CONFIRM_DIGEST,
            link=f"/events/{event.id}",
            event_id=event.id,
            event_title=event.title,
            attendees=candidates,
            attendee_count=len(candidates),
            ends_at=event.ends_at,
        )
        notification = await notify(
            session,
            recipient,
            payload,
            now,
            dedupe_key=f"attendance_confirm_digest:{recipient}:{event.id}",
        )
        if notification is not None:
            created += 1
    return created


async def send_pending_attendance_digests(
    session: AsyncSession, now: datetime, *, limit: int = 50
) -> int:
    claim_days = (await load_settings(session)).attendance_claim_days
    event_ids = (
        await session.scalars(
            select(models.Event.id)
            .where(
                models.Event.status.in_(("published", "archived")),
                models.Event.ends_at <= now,
                models.Event.ends_at > now - timedelta(days=claim_days),
                select(models.EventAttendance.id)
                .where(
                    models.EventAttendance.event_id == models.Event.id,
                    models.EventAttendance.status == "attended",
                )
                .exists(),
            )
            .order_by(models.Event.ends_at, models.Event.id)
            .limit(limit)
        )
    ).all()
    created = 0
    for event_id in event_ids:
        created += await send_attendance_digest(session, event_id, now)
    return created


async def send_contributions_digest(session: AsyncSession, release_id: UUID, now: datetime) -> int:
    release = await session.get(models.ProjectRelease, release_id)
    if release is None:
        return 0
    project = await session.get(models.Project, release.project_id)
    if project is None or project.lead_user_id is None:
        return 0
    lead = await session.get(models.User, project.lead_user_id)
    if lead is None or lead.banned_at is not None:
        return 0
    lead_member = await session.scalar(
        select(models.ProjectMember).where(
            models.ProjectMember.project_id == project.id,
            models.ProjectMember.user_id == lead.id,
            models.ProjectMember.state == "confirmed",
            models.ProjectMember.left_at.is_(None),
        )
    )
    if lead_member is None:
        return 0
    recipients = (
        await session.scalars(
            select(models.ProjectMember.user_id)
            .join(models.User)
            .where(
                models.ProjectMember.project_id == project.id,
                models.ProjectMember.user_id != lead.id,
                models.ProjectMember.state == "confirmed",
                models.ProjectMember.left_at.is_(None),
                models.User.banned_at.is_(None),
            )
            .order_by(models.ProjectMember.user_id)
        )
    ).all()
    payload = ContributionsConfirmDigestPayload(
        type=NotificationType.CONTRIBUTIONS_CONFIRM_DIGEST,
        link=f"/projects/{project.id}",
        project_id=project.id,
        project_name=project.name,
        release_id=release.id,
        release_title=release.title,
        lead=NotificationPerson(user_id=lead.id, display_name=lead.display_name),
        project_member_id=lead_member.id,
    )
    created = 0
    for recipient in recipients:
        notification = await notify(
            session,
            recipient,
            payload,
            now,
            dedupe_key=f"contributions_confirm_digest:{recipient}:{release.id}",
        )
        if notification is not None:
            created += 1
    return created
