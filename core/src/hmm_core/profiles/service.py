from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, cast
from uuid import UUID

import procrastinate
from hmm_contracts.algo import MentorOffer, ReviewIn
from hmm_contracts.common import (
    ErrorCode,
    FlagStatus,
    FlagSubjectType,
    GithubSnapshotStatus,
    Locale,
    MemberReviewStatus,
    ReviewContext,
    ReviewSubjectType,
)
from sqlalchemy import Numeric, Text, and_, exists, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.identity.service import ONBOARDED, dnd_active, dnd_until, lock_user
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.jobs import JobName, defer_job
from hmm_core.platform.pagination import Column, CursorParams, apply_keyset, encode_cursor
from hmm_core.platform.schemas import AreaScoreSummary
from hmm_core.profiles import schemas
from hmm_core.profiles.github import github_summary
from hmm_core.profiles.schemas import UpdateProfileBody
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


async def get_user(session: AsyncSession, user_id: UUID) -> models.User:
    user = await session.get(models.User, user_id)
    if user is None or user.onboarding_status not in ONBOARDED or user.banned_at is not None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return user


async def visible_reviews(session: AsyncSession, user_id: UUID) -> list[ReviewIn]:
    flags = list(
        await session.scalars(
            select(models.Flag).where(
                models.Flag.subject_type == FlagSubjectType.REVIEW.value,
                models.Flag.subject_id.in_(
                    select(models.Review.id).where(models.Review.reviewee_id == user_id)
                ),
            )
        )
    )
    to_check = {flag.subject_id for flag in flags if flag.status == FlagStatus.TO_CHECK.value}
    pardoned = {flag.subject_id for flag in flags if flag.status == FlagStatus.PARDONED.value}
    rows = await session.scalars(
        select(models.Review)
        .where(
            models.Review.reviewee_id == user_id,
            models.Review.revealed_at.is_not(None),
            models.Review.removed_at.is_(None),
            models.Review.context.in_(
                [ReviewContext.MEMBER.value, ReviewContext.LEAD.value, ReviewContext.MENTOR.value]
            ),
            models.Review.id.not_in(to_check),
        )
        .order_by(models.Review.created_at.desc(), models.Review.id.desc())
        .limit(20)
    )
    return [
        ReviewIn(
            id=row.id,
            reviewer_id=row.reviewer_id,
            reviewee_id=row.reviewee_id,
            context=ReviewContext(row.context),
            rating=row.rating,
            subject_type=ReviewSubjectType(row.subject_type),
            subject_id=row.subject_id,
            comment=row.comment,
            created_at=row.created_at,
            revealed_at=row.revealed_at,
            removed_at=row.removed_at,
            flag_status=FlagStatus.PARDONED if row.id in pardoned else None,
        )
        for row in rows
    ]


async def user_profile(
    session: AsyncSession, user: models.User, actor: models.User, now: datetime
) -> schemas.UserProfile:
    summary = (await user_summaries(session, [user], actor, now))[0]
    snapshot = await session.get(models.GithubSnapshot, user.id)
    scores_reviewed_at = await session.scalar(
        select(models.MemberReview.reviewed_at).where(
            models.MemberReview.user_id == user.id,
            models.MemberReview.status == MemberReviewStatus.REVIEWED.value,
        )
    )
    return schemas.UserProfile(
        **summary.model_dump(),
        about_md=user.about_md,
        contacts=cast(dict[str, str], user.contacts),
        timezone=user.timezone,
        locale=Locale(user.locale),
        github=github_summary(snapshot),
        github_status=GithubSnapshotStatus(snapshot.status) if snapshot is not None else None,
        scores_reviewed_at=scores_reviewed_at,
        reviews=await visible_reviews(session, user.id),
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


async def update_profile(
    session: AsyncSession, jobs: procrastinate.App, actor: models.User, body: UpdateProfileBody
) -> models.User:
    target = await lock_user(session, actor.id)
    provided = body.model_dump(exclude_unset=True)
    for field in ("display_name", "about_md", "contacts", "locale"):
        if field in provided and provided[field] is None:
            raise ProblemError(ErrorCode.VALIDATION_ERROR, f"{field} cannot be null")
    changed = {key: value for key, value in provided.items() if getattr(target, key) != value}
    if not changed:
        return target
    for key, value in changed.items():
        setattr(target, key, value)
    await session.flush()
    await session.refresh(target)
    await defer_job(session, jobs, JobName.RECOMPUTE_MEMBER, user_id=str(target.id))
    return target
