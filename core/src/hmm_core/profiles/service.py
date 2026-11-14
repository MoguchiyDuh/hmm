from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, cast
from uuid import UUID

from hmm_contracts.algo import MentorOffer
from hmm_contracts.common import ErrorCode, ReviewContext
from sqlalchemy import Numeric, Text, and_, exists, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.identity.service import ONBOARDED, dnd_active, dnd_until
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.pagination import Column, CursorParams, apply_keyset, encode_cursor
from hmm_core.platform.schemas import AreaScoreSummary
from hmm_core.profiles import schemas
from hmm_core.scores.reputation import reputation_summaries


@dataclass(frozen=True, slots=True, kw_only=True)
class DirectoryQuery:
    q: str | None
    area_key: str | None
    min_score: float | None
    available: bool | None
    mentor: bool | None
    sort: Literal["xp", "activity", "score", "reputation", "name"]


async def load_area_by_key(session: AsyncSession, key: str) -> models.Area:
    area = await session.scalar(select(models.Area).where(models.Area.key == key))
    if area is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return area


async def list_directory(
    session: AsyncSession, pagination: CursorParams, query: DirectoryQuery, now: datetime
) -> tuple[list[models.User], str | None]:
    area = await load_area_by_key(session, query.area_key) if query.area_key is not None else None
    if query.sort == "score" and area is None:
        raise ProblemError(ErrorCode.VALIDATION_ERROR, "sort=score requires area")
    reputation = (
        select(
            models.Review.reviewee_id.label("user_id"),
            func.avg(models.Review.rating, type_=Numeric).label("avg_rating"),
            func.count(models.Review.rating).label("rating_count"),
        )
        .where(
            models.Review.context == ReviewContext.MEMBER.value,
            models.Review.rating.is_not(None),
            models.Review.revealed_at.is_not(None),
            models.Review.removed_at.is_(None),
        )
        .group_by(models.Review.reviewee_id)
        .subquery("member_reputation")
    )
    descending = query.sort != "name"
    match query.sort:
        case "activity":
            key_columns = [func.coalesce(models.MemberMetric.activity, 0)]
        case "name":
            key_columns = [func.lower(models.User.display_name, type_=Text)]
        case "score":
            key_columns = [func.coalesce(models.AreaScore.override, models.AreaScore.calculated, 0)]
        case "reputation":
            key_columns = [
                func.coalesce(reputation.c.avg_rating, 0),
                func.coalesce(reputation.c.rating_count, 0),
            ]
        case "xp":
            key_columns = [func.coalesce(models.MemberMetric.xp, 0)]
    labels = [column.label(f"sort_{index}") for index, column in enumerate(key_columns)]
    statement = (
        select(models.User)
        .add_columns(*labels)
        .outerjoin(models.MemberMetric, models.MemberMetric.user_id == models.User.id)
        .where(models.User.onboarding_status.in_(ONBOARDED), models.User.banned_at.is_(None))
    )
    if query.sort == "score" and area is not None:
        statement = statement.outerjoin(
            models.AreaScore,
            and_(models.AreaScore.user_id == models.User.id, models.AreaScore.area_id == area.id),
        )
    elif query.sort == "reputation":
        statement = statement.outerjoin(reputation, reputation.c.user_id == models.User.id)
    if query.q is not None and (term := query.q.strip()):
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        statement = statement.where(
            or_(
                models.User.display_name.ilike(pattern, escape="\\"),
                models.User.github_login.ilike(pattern, escape="\\"),
            )
        )
    if area is not None:
        statement = statement.where(
            models.User.id.in_(
                select(models.AreaScore.user_id).where(models.AreaScore.area_id == area.id)
            )
        )
    if query.min_score is not None:
        minimum = Decimal(str(query.min_score))
        if area is not None:
            statement = statement.where(
                models.User.id.in_(
                    select(models.AreaScore.user_id).where(
                        models.AreaScore.area_id == area.id,
                        func.coalesce(models.AreaScore.override, models.AreaScore.calculated)
                        >= minimum,
                    )
                )
            )
        else:
            best = (
                select(
                    func.max(func.coalesce(models.AreaScore.override, models.AreaScore.calculated))
                )
                .join(models.Area, models.Area.id == models.AreaScore.area_id)
                .where(
                    models.AreaScore.user_id == models.User.id, models.Area.archived_at.is_(None)
                )
                .correlate(models.User)
                .scalar_subquery()
            )
            statement = statement.where(best >= minimum)
    if query.available is not None:
        active_dnd = or_(
            models.User.dnd_indefinite.is_(True),
            and_(models.User.dnd_until.is_not(None), models.User.dnd_until > now),
        )
        statement = statement.where(not_(active_dnd) if query.available else active_dnd)
    if query.mentor is not None:
        active_offer = exists().where(
            models.MentorOffer.user_id == models.User.id, models.MentorOffer.active.is_(True)
        )
        statement = statement.where(active_offer if query.mentor else not_(active_offer))
    rows = list(
        await session.execute(
            apply_keyset(
                statement,
                pagination,
                sort=cast(Sequence[Column], key_columns),
                id_column=models.User.id,
                descending=descending,
            )
        )
    )
    users = [row[0] for row in rows[: pagination.limit]]
    next_cursor = None
    if len(rows) > pagination.limit:
        last = rows[pagination.limit - 1]
        next_cursor = encode_cursor(
            [*(last._mapping[f"sort_{index}"] for index in range(len(labels))), last[0].id]
        )
    return users, next_cursor


async def user_summaries(
    session: AsyncSession, users: Sequence[models.User], actor: models.User, now: datetime
) -> list[schemas.UserSummary]:
    ids = [user.id for user in users]
    if not ids:
        return []
    metrics = {
        row.user_id: row
        for row in await session.scalars(
            select(models.MemberMetric).where(models.MemberMetric.user_id.in_(ids))
        )
    }
    scores: dict[UUID, list[AreaScoreSummary]] = {}
    for score, area in await session.execute(
        select(models.AreaScore, models.Area)
        .join(models.Area, models.Area.id == models.AreaScore.area_id)
        .where(models.AreaScore.user_id.in_(ids), models.Area.archived_at.is_(None))
        .order_by(models.Area.sort, models.Area.key)
    ):
        scores.setdefault(score.user_id, []).append(
            AreaScoreSummary(
                area_key=area.key,
                effective=float(score.override if score.override is not None else score.calculated),
                reason=score.override_reason
                if score.override is not None and score.override_reason
                else score.calc_reason,
            )
        )
    offers: dict[UUID, list[MentorOffer]] = {}
    for offer, area in await session.execute(
        select(models.MentorOffer, models.Area)
        .join(models.Area, models.Area.id == models.MentorOffer.area_id)
        .where(models.MentorOffer.user_id.in_(ids))
        .order_by(models.Area.sort, models.Area.key)
    ):
        offers.setdefault(offer.user_id, []).append(
            MentorOffer(area=area.key, topics=offer.topics, active=offer.active)
        )
    reputation = await reputation_summaries(session, ids)
    result = []
    for user in users:
        metric = metrics.get(user.id)
        result.append(
            schemas.UserSummary(
                id=user.id,
                display_name=user.display_name,
                avatar_url=user.avatar_url,
                github_login=user.github_login,
                github_url=f"https://github.com/{user.github_login}",
                area_scores=scores.get(user.id, []),
                xp=float(metric.xp) if metric is not None else 0.0,
                activity=float(metric.activity) if metric is not None else 0.0,
                reputation=reputation[user.id],
                dnd_until=dnd_until(user),
                mentor_offers=offers.get(user.id, []),
                allowed_actions=[schemas.UserAction.REQUEST_HELP]
                if actor.id != user.id and not dnd_active(user, now)
                else [],
            )
        )
    return result
