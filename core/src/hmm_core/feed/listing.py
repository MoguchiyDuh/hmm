from datetime import datetime, timedelta
from uuid import UUID

from hmm_contracts.common import AreaKey, ErrorCode
from pydantic import JsonValue, TypeAdapter
from sqlalchemy import ARRAY, Uuid, and_, cast, false, func, not_, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.feed import schemas
from hmm_core.feed.schemas import FeedItemAction, FeedKind
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.pagination import CursorParams, apply_keyset, encode_cursor
from hmm_core.platform.schemas import UserRef
from hmm_core.platform.settings import RuntimeSettings, load_settings


async def _mentor_areas(
    session: AsyncSession, viewer: models.User, settings: RuntimeSettings, now: datetime
) -> list[UUID]:
    if viewer.dnd_indefinite or (viewer.dnd_until is not None and viewer.dnd_until > now):
        return []
    restricted = await session.scalar(
        select(models.MemberRestriction.id)
        .where(
            models.MemberRestriction.user_id == viewer.id,
            models.MemberRestriction.kind == "mentoring",
            models.MemberRestriction.lifted_at.is_(None),
            or_(models.MemberRestriction.until.is_(None), models.MemberRestriction.until > now),
        )
        .limit(1)
    )
    if restricted is not None:
        return []
    active_count = await session.scalar(
        select(func.count())
        .select_from(models.Mentorship)
        .where(
            models.Mentorship.mentor_id == viewer.id,
            models.Mentorship.status.in_(("active", "stalled", "closing")),
        )
    )
    capacity = (
        viewer.mentor_capacity
        if viewer.mentor_capacity is not None
        else settings.mentor_capacity_default
    )
    if active_count is not None and active_count >= capacity:
        return []
    return list(
        await session.scalars(
            select(models.MentorOffer.area_id).where(
                models.MentorOffer.user_id == viewer.id, models.MentorOffer.active
            )
        )
    )


async def _allowed_actions(
    session: AsyncSession, viewer: models.User, rows: list[models.FeedItem], now: datetime
) -> dict[tuple[str, UUID], list[FeedItemAction]]:
    actions: dict[tuple[str, UUID], list[FeedItemAction]] = {}
    event_ids = {
        row.subject_id for row in rows if row.kind in (FeedKind.EVENT_GOING, FeedKind.EVENT_NEW)
    }
    if event_ids:
        attended = (
            select(models.EventAttendance.id)
            .where(
                models.EventAttendance.event_id == models.Event.id,
                models.EventAttendance.user_id == viewer.id,
                models.EventAttendance.status.in_(("going", "attended")),
            )
            .exists()
        )
        ids = await session.scalars(
            select(models.Event.id).where(
                models.Event.id.in_(event_ids),
                models.Event.status == "published",
                models.Event.ends_at > now,
                not_(attended),
            )
        )
        for event_id in ids:
            for kind in (FeedKind.EVENT_GOING, FeedKind.EVENT_NEW):
                actions[kind, event_id] = [FeedItemAction.GOING]
    project_ids = {row.subject_id for row in rows if row.kind == FeedKind.PROJECT_RECRUITING}
    if project_ids:
        member = (
            select(models.ProjectMember.id)
            .where(
                models.ProjectMember.project_id == models.Project.id,
                models.ProjectMember.user_id == viewer.id,
                models.ProjectMember.state == "confirmed",
                models.ProjectMember.left_at.is_(None),
            )
            .exists()
        )
        pending = (
            select(models.JoinRequest.id)
            .where(
                models.JoinRequest.project_id == models.Project.id,
                models.JoinRequest.user_id == viewer.id,
                models.JoinRequest.status == "pending",
            )
            .exists()
        )
        ids = await session.scalars(
            select(models.Project.id).where(
                models.Project.id.in_(project_ids),
                models.Project.status.in_(("idea", "active")),
                models.Project.recruiting.is_not(None),
                or_(
                    models.Project.lead_user_id.is_(None), models.Project.lead_user_id != viewer.id
                ),
                not_(member),
                not_(pending),
            )
        )
        for project_id in ids:
            actions[FeedKind.PROJECT_RECRUITING, project_id] = [FeedItemAction.REQUEST_JOIN]
    request_ids = {row.subject_id for row in rows if row.kind == FeedKind.HELP_REQUEST}
    if request_ids:
        offered = (
            select(models.HelpOffer.id)
            .where(
                models.HelpOffer.request_id == models.HelpRequest.id,
                models.HelpOffer.mentor_id == viewer.id,
            )
            .exists()
        )
        ids = await session.scalars(
            select(models.HelpRequest.id).where(
                models.HelpRequest.id.in_(request_ids),
                models.HelpRequest.status == "open",
                models.HelpRequest.learner_id != viewer.id,
                not_(offered),
            )
        )
        for request_id in ids:
            actions[FeedKind.HELP_REQUEST, request_id] = [FeedItemAction.OFFER_HELP]
    return actions


async def list_feed(
    session: AsyncSession,
    viewer: models.User,
    params: CursorParams,
    *,
    kind: FeedKind | None,
    area: AreaKey | None,
    mine_areas: bool,
    now: datetime,
) -> schemas.FeedItemPage:
    settings = await load_settings(session)
    statement = select(models.FeedItem).where(models.FeedItem.hidden_at.is_(None))
    if kind is not None:
        statement = statement.where(models.FeedItem.kind == kind)
    if area is not None:
        area_id = await session.scalar(select(models.Area.id).where(models.Area.key == area))
        if area_id is None:
            raise ProblemError(ErrorCode.NOT_FOUND)
        statement = statement.where(models.FeedItem.area_ids.op("&&")(cast([area_id], ARRAY(Uuid))))
    member = (
        select(models.ProjectMember.id)
        .where(
            models.ProjectMember.project_id == models.Project.id,
            models.ProjectMember.user_id == viewer.id,
            models.ProjectMember.state == "confirmed",
            models.ProjectMember.left_at.is_(None),
        )
        .exists()
    )
    project_visible = (
        true() if viewer.is_admin else or_(models.Project.visibility == "public", member)
    )
    flagged = (
        select(models.Flag.id)
        .where(
            models.Flag.status == "to_check",
            models.Flag.subject_type == "mentorship",
            models.Flag.subject_id == models.FeedItem.subject_id,
        )
        .correlate(models.FeedItem)
        .exists()
    )
    statement = statement.where(
        or_(
            and_(
                models.FeedItem.kind == FeedKind.PROJECT_RECRUITING,
                select(models.Project.id)
                .where(
                    models.Project.id == models.FeedItem.subject_id,
                    models.Project.status.in_(("idea", "active")),
                    models.Project.recruiting.is_not(None),
                    project_visible,
                )
                .exists(),
            ),
            and_(
                models.FeedItem.kind == FeedKind.PROJECT_SHIPPED,
                select(models.Project.id)
                .where(models.Project.id == models.FeedItem.subject_id, project_visible)
                .exists(),
            ),
            and_(
                models.FeedItem.kind == FeedKind.EVENT_GOING,
                select(models.Event.id)
                .where(
                    models.Event.id == models.FeedItem.subject_id,
                    models.Event.status.in_(("published", "archived")),
                    models.Event.ends_at > now - timedelta(days=settings.attendance_claim_days),
                )
                .exists(),
            ),
            and_(
                models.FeedItem.kind == FeedKind.EVENT_NEW,
                select(models.Event.id)
                .where(
                    models.Event.id == models.FeedItem.subject_id,
                    models.Event.status.in_(("published", "archived")),
                )
                .exists(),
            ),
            and_(
                models.FeedItem.kind == FeedKind.HELP_REQUEST,
                select(models.HelpRequest.id)
                .where(
                    models.HelpRequest.id == models.FeedItem.subject_id,
                    models.HelpRequest.status == "open",
                )
                .exists(),
            ),
            and_(
                models.FeedItem.kind == FeedKind.MENTORSHIP_COMPLETED,
                select(models.Mentorship.id)
                .where(
                    models.Mentorship.id == models.FeedItem.subject_id,
                    models.Mentorship.status == "closed",
                    models.Mentorship.outcome == "reached",
                    not_(flagged),
                )
                .exists(),
            ),
        )
    )
    if mine_areas:
        top_areas = list(
            await session.scalars(
                select(models.AreaScore.area_id)
                .join(models.Area, models.Area.id == models.AreaScore.area_id)
                .where(models.AreaScore.user_id == viewer.id, models.Area.archived_at.is_(None))
                .order_by(
                    func.coalesce(models.AreaScore.override, models.AreaScore.calculated).desc(),
                    models.Area.sort,
                    models.Area.key,
                )
                .limit(3)
            )
        )
        mentor_areas = await _mentor_areas(session, viewer, settings, now)
        statement = statement.where(
            or_(
                models.FeedItem.area_ids.op("&&")(cast(top_areas, ARRAY(Uuid))),
                and_(
                    models.FeedItem.kind == FeedKind.HELP_REQUEST,
                    models.FeedItem.actor_id != viewer.id,
                    models.FeedItem.area_ids.op("&&")(cast(mentor_areas, ARRAY(Uuid))),
                ),
            )
            if top_areas or mentor_areas
            else false()
        )
    rows = list(
        await session.scalars(
            apply_keyset(
                statement, params, sort=[models.FeedItem.created_at], id_column=models.FeedItem.id
            )
        )
    )
    next_cursor = None
    if len(rows) > params.limit:
        last = rows[params.limit - 1]
        next_cursor = encode_cursor([last.created_at, last.id])
    rows = rows[: params.limit]
    event_ids = {row.subject_id for row in rows if row.kind == FeedKind.EVENT_GOING}
    counts: dict[UUID, int] = {}
    if event_ids:
        counts = {
            event_id: count
            for event_id, count in await session.execute(
                select(models.EventAttendance.event_id, func.count())
                .join(models.User, models.User.id == models.EventAttendance.user_id)
                .where(
                    models.EventAttendance.event_id.in_(event_ids),
                    models.EventAttendance.status == "going",
                    models.User.banned_at.is_(None),
                    not_(models.User.dnd_indefinite),
                    or_(models.User.dnd_until.is_(None), models.User.dnd_until <= now),
                )
                .group_by(models.EventAttendance.event_id)
            )
        }
    rows = [
        row for row in rows if row.kind != FeedKind.EVENT_GOING or counts.get(row.subject_id, 0) > 0
    ]
    actor_ids = {row.actor_id for row in rows if row.actor_id is not None}
    actors = (
        {
            actor.id: UserRef.model_validate(actor)
            for actor in await session.scalars(
                select(models.User).where(models.User.id.in_(actor_ids))
            )
        }
        if actor_ids
        else {}
    )
    actions = await _allowed_actions(session, viewer, rows, now)
    items: list[schemas.FeedItem] = []
    for row in rows:
        payload = TypeAdapter(dict[str, JsonValue]).validate_python(row.payload)
        if row.kind == FeedKind.EVENT_GOING:
            payload = {**payload, "count": counts[row.subject_id]}
        items.append(
            schemas.FeedItem(
                id=row.id,
                kind=FeedKind(row.kind),
                subject_type=row.subject_type,
                subject_id=row.subject_id,
                actor_id=row.actor_id,
                actor=actors.get(row.actor_id) if row.actor_id is not None else None,
                area_ids=row.area_ids,
                payload=payload,
                created_at=row.created_at,
                hidden_at=row.hidden_at,
                allowed_actions=actions.get((row.kind, row.subject_id), []),
            )
        )
    return schemas.FeedItemPage(items=items, next_cursor=next_cursor)
