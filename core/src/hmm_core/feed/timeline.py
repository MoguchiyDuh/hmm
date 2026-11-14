from datetime import datetime
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models


async def record_project_recruiting(
    session: AsyncSession, project: models.Project, actor_id: UUID, now: datetime
) -> None:
    area_ids = list(
        await session.scalars(
            select(models.ProjectArea.area_id)
            .where(models.ProjectArea.project_id == project.id)
            .order_by(models.ProjectArea.area_id)
        )
    )
    live = (
        models.FeedItem.kind == "project_recruiting",
        models.FeedItem.subject_type == "project",
        models.FeedItem.subject_id == project.id,
        models.FeedItem.hidden_at.is_(None),
    )
    if (
        project.recruiting is None
        or project.status not in ("idea", "active")
        or project.visibility != "public"
    ):
        await session.execute(update(models.FeedItem).where(*live).values(hidden_at=now))
        return
    payload = {"project_id": str(project.id), "name": project.name, "goal": project.goal}
    newest = await session.scalar(
        select(models.FeedItem)
        .where(*live)
        .order_by(models.FeedItem.created_at.desc(), models.FeedItem.id.desc())
        .limit(1)
    )
    if newest is not None and newest.area_ids == area_ids and newest.payload == payload:
        return
    await session.execute(update(models.FeedItem).where(*live).values(hidden_at=now))
    session.add(
        models.FeedItem(
            kind="project_recruiting",
            subject_type="project",
            subject_id=project.id,
            actor_id=actor_id,
            area_ids=area_ids,
            payload=payload,
            created_at=now,
            hidden_at=None,
        )
    )
    await session.flush()


async def record_project_shipped(
    session: AsyncSession,
    project: models.Project,
    release: models.ProjectRelease | None,
    actor_id: UUID,
    now: datetime,
) -> None:
    if project.status != "shipped":
        return
    release_id = str(release.id) if release is not None else None
    if await session.scalar(
        select(
            exists().where(
                models.FeedItem.kind == "project_shipped",
                models.FeedItem.subject_type == "project",
                models.FeedItem.subject_id == project.id,
                models.FeedItem.hidden_at.is_(None),
                models.FeedItem.payload["release_id"].as_string() == release_id,
            )
        )
    ):
        return
    area_ids = list(
        await session.scalars(
            select(models.ProjectArea.area_id)
            .where(models.ProjectArea.project_id == project.id)
            .order_by(models.ProjectArea.area_id)
        )
    )
    session.add(
        models.FeedItem(
            kind="project_shipped",
            subject_type="project",
            subject_id=project.id,
            actor_id=actor_id,
            area_ids=area_ids,
            payload={
                "project_id": str(project.id),
                "name": project.name,
                "release_id": release_id,
                "release_title": release.title if release is not None else None,
            },
            created_at=now,
            hidden_at=None,
        )
    )
    await session.flush()


async def record_help_request(
    session: AsyncSession, request: models.HelpRequest, actor_id: UUID, now: datetime
) -> None:
    if request.status != "open":
        return
    if await session.scalar(
        select(
            exists().where(
                models.FeedItem.kind == "help_request",
                models.FeedItem.subject_type == "help_request",
                models.FeedItem.subject_id == request.id,
                models.FeedItem.hidden_at.is_(None),
            )
        )
    ):
        return
    area_key = await session.scalar(
        select(models.Area.key).where(models.Area.id == request.area_id)
    )
    session.add(
        models.FeedItem(
            kind="help_request",
            subject_type="help_request",
            subject_id=request.id,
            actor_id=actor_id,
            area_ids=[request.area_id],
            payload={
                "request_id": str(request.id),
                "area_key": area_key,
                "topic": request.topic,
                "goal": request.goal,
                "learner_id": str(request.learner_id),
            },
            created_at=now,
            hidden_at=None,
        )
    )
    await session.flush()


async def record_event_going(
    session: AsyncSession, event_id: UUID, member_id: UUID, now: datetime
) -> None:
    event = await session.scalar(
        select(models.Event).where(models.Event.id == event_id).with_for_update()
    )
    if event is None or event.status != "published":
        return
    area_ids = list(
        await session.scalars(
            select(models.EventArea.area_id)
            .where(models.EventArea.event_id == event.id)
            .order_by(models.EventArea.area_id)
        )
    )
    count = await session.scalar(
        select(func.count())
        .select_from(models.EventAttendance)
        .join(models.User, models.User.id == models.EventAttendance.user_id)
        .where(
            models.EventAttendance.event_id == event.id,
            models.EventAttendance.status == "going",
            models.User.banned_at.is_(None),
            models.User.dnd_indefinite.is_(False),
            or_(models.User.dnd_until.is_(None), models.User.dnd_until <= now),
        )
    )
    item = await session.scalar(
        select(models.FeedItem)
        .where(
            models.FeedItem.kind == "event_going",
            models.FeedItem.subject_type == "event",
            models.FeedItem.subject_id == event.id,
            models.FeedItem.hidden_at.is_(None),
            models.FeedItem.created_at >= now.replace(hour=0, minute=0, second=0, microsecond=0),
        )
        .order_by(models.FeedItem.created_at, models.FeedItem.id)
        .limit(1)
        .with_for_update()
    )
    payload: dict[str, JsonValue] = {
        "event_id": str(event.id),
        "event_title": event.title,
        "count": count or 0,
    }
    if item is not None:
        item.payload = payload
    else:
        session.add(
            models.FeedItem(
                kind="event_going",
                subject_type="event",
                subject_id=event.id,
                actor_id=member_id,
                area_ids=area_ids,
                payload=payload,
                created_at=now,
                hidden_at=None,
            )
        )
    if not await session.scalar(
        select(
            exists().where(
                models.FeedItem.kind == "event_new",
                models.FeedItem.subject_type == "event",
                models.FeedItem.subject_id == event.id,
            )
        )
    ):
        session.add(
            models.FeedItem(
                kind="event_new",
                subject_type="event",
                subject_id=event.id,
                actor_id=member_id,
                area_ids=area_ids,
                payload={"event_id": str(event.id), "event_title": event.title},
                created_at=now,
                hidden_at=None,
            )
        )
    await session.flush()


async def record_mentorship_completed(
    session: AsyncSession, mentorship_id: UUID, now: datetime
) -> None:
    mentorship = await session.get(models.Mentorship, mentorship_id)
    if mentorship is None or mentorship.status != "closed" or mentorship.outcome != "reached":
        return
    if await session.scalar(
        select(
            exists().where(
                models.FeedItem.kind == "mentorship_completed",
                models.FeedItem.subject_type == "mentorship",
                models.FeedItem.subject_id == mentorship.id,
            )
        )
    ):
        return
    mentor = await session.get(models.User, mentorship.mentor_id)
    learner = await session.get(models.User, mentorship.learner_id)
    if mentor is None or learner is None:
        return
    area_key = await session.scalar(
        select(models.Area.key).where(models.Area.id == mentorship.area_id)
    )
    session.add(
        models.FeedItem(
            kind="mentorship_completed",
            subject_type="mentorship",
            subject_id=mentorship.id,
            actor_id=None,
            area_ids=[mentorship.area_id],
            payload={
                "mentorship_id": str(mentorship.id),
                "area_key": area_key,
                "mentor": {"user_id": str(mentor.id), "display_name": mentor.display_name},
                "learner": {"user_id": str(learner.id), "display_name": learner.display_name},
            },
            created_at=now,
            hidden_at=None,
        )
    )
    await session.flush()


async def hide_feed_subject(
    session: AsyncSession, subject_type: str, subject_id: UUID, now: datetime
) -> int:
    hidden = await session.scalars(
        update(models.FeedItem)
        .where(
            models.FeedItem.subject_type == subject_type,
            models.FeedItem.subject_id == subject_id,
            models.FeedItem.hidden_at.is_(None),
        )
        .values(hidden_at=now)
        .returning(models.FeedItem.id)
    )
    return len(hidden.all())
