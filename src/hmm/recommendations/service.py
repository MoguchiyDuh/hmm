from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import UUID

import procrastinate
from pydantic import ValidationError
from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm import models
from hmm.algo.models import (
    Rec,
    Recommendations,
)
from hmm.common import (
    ErrorCode,
    HelpRequestStatus,
    RecommendationStatus,
    RecommendationSubjectType,
)
from hmm.formation import common as formation_common
from hmm.platform.db import get_or_404, locked
from hmm.platform.errors import ProblemError
from hmm.platform.jobs import JobName, defer_job
from hmm.platform.schemas import user_ref
from hmm.recommendations import schemas
from hmm.recommendations.candidates import (
    RecommendationContext,
    eligible_user,
    recommendation_context,
)

REFRESH_INTERVAL = timedelta(minutes=1)


def recommendation_refreshable(project: models.Project) -> bool:
    if project.team_status is not None:
        return project.team_status in {
            state.value for state in formation_common.CAPACITY_TEAM_STATES
        }
    return project.recruiting is not None


async def latest_recommendation_set(
    session: AsyncSession,
    subject_type: RecommendationSubjectType,
    subject_id: UUID,
    *,
    ready: bool = False,
) -> models.RecommendationSet | None:
    query = select(models.RecommendationSet).where(
        models.RecommendationSet.subject_type == subject_type.value,
        models.RecommendationSet.subject_id == subject_id,
    )
    if ready:
        query = query.where(models.RecommendationSet.status == RecommendationStatus.READY.value)
    return await session.scalar(
        query.order_by(
            models.RecommendationSet.created_at.desc(), models.RecommendationSet.id.desc()
        ).limit(1)
    )


def stored_recs(items: object) -> list[Rec]:
    if not isinstance(items, list):
        return []
    return [Rec.model_validate(item) for item in items]


async def project_recommendations(
    session: AsyncSession, project_id: UUID, actor: models.User, now: datetime
) -> schemas.ProjectRecommendations:
    project = await get_or_404(session, models.Project, project_id)
    if actor.id != project.lead_user_id and not actor.is_admin:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if not recommendation_refreshable(project):
        raise ProblemError(ErrorCode.INVALID_STATE, "Quick-pick is not available for this project")
    row = await latest_recommendation_set(session, RecommendationSubjectType.PROJECT, project.id)
    served = await latest_recommendation_set(
        session, RecommendationSubjectType.PROJECT, project.id, ready=True
    )
    status = RecommendationStatus(row.status) if row is not None else RecommendationStatus.PENDING
    stale = (
        served is None
        or (await recommendation_context(session, project, now, served.algo_version)).input_hash
        != served.input_hash
    )
    computed_at = served.completed_at if served is not None else None
    enrolled_recs = stored_recs(served.enrolled) if served is not None else []
    others_recs = stored_recs(served.others) if served is not None else []
    user_ids = {rec.user_id for rec in (*enrolled_recs, *others_recs)}
    users = (
        {
            user.id: user
            for user in await session.scalars(
                select(models.User).where(models.User.id.in_(user_ids))
            )
        }
        if user_ids
        else {}
    )

    def build(recs: list[Rec]) -> list[schemas.Recommendation]:
        return [
            schemas.Recommendation(
                **rec.model_dump(),
                profile=user_ref(users[rec.user_id]),
            )
            for rec in recs
            if rec.user_id in users
        ]

    return schemas.ProjectRecommendations(
        status=status,
        stale=stale,
        computed_at=computed_at,
        enrolled=build(enrolled_recs),
        others=build(others_recs),
        allowed_actions=[schemas.RecommendationsAction.REFRESH]
        if actor.id == project.lead_user_id
        else [],
    )


async def prepare_refresh(
    session: AsyncSession, project: models.Project, now: datetime, algo_version: str
) -> tuple[UUID, RecommendationContext] | None:
    if not recommendation_refreshable(project):
        return None
    context = await recommendation_context(session, project, now, algo_version)
    previous = await latest_recommendation_set(
        session, RecommendationSubjectType.PROJECT, project.id, ready=True
    )
    if previous is not None and previous.input_hash == context.input_hash:
        return None
    row = models.RecommendationSet(
        subject_type=RecommendationSubjectType.PROJECT.value,
        subject_id=project.id,
        input_hash=context.input_hash,
        status=RecommendationStatus.PENDING.value,
        error=None,
        enrolled=None,
        others=None,
        algo_version=algo_version,
        completed_at=None,
    )
    session.add(row)
    await session.flush()
    return row.id, context


async def mark_ready(
    session: AsyncSession, row_id: UUID, result: Recommendations, now: datetime
) -> None:
    row = await locked(session, models.RecommendationSet, row_id)
    if row is None:
        return None
    row.status = RecommendationStatus.READY.value
    row.enrolled = [rec.model_dump(mode="json") for rec in result.enrolled]
    row.others = [rec.model_dump(mode="json") for rec in result.others]
    row.error = None
    row.completed_at = now


async def mark_failed(session: AsyncSession, row_id: UUID, error: Exception, now: datetime) -> None:
    row = await locked(session, models.RecommendationSet, row_id)
    if row is None:
        return None
    row.status = RecommendationStatus.FAILED.value
    row.error = str(error)[:1000]
    row.completed_at = now


def validated_group(items: Sequence[Rec], ids: frozenset[UUID]) -> tuple[list[Rec], int]:
    kept: list[Rec] = []
    seen: set[UUID] = set()
    dropped = 0
    for item in items:
        if item.user_id not in ids or item.user_id in seen:
            dropped += 1
            continue
        reasons = [
            reason.strip() for reason in item.reasons if isinstance(reason, str) and reason.strip()
        ][:3]
        if not reasons:
            dropped += 1
            continue
        try:
            rec = Rec(
                user_id=item.user_id,
                score=min(1.0, max(0.0, float(item.score))),
                reasons=reasons,
                tags=[str(tag) for tag in item.tags],
                suggested_area=item.suggested_area,
            )
        except ValidationError:
            dropped += 1
            continue
        seen.add(item.user_id)
        kept.append(rec)
    return kept, dropped


def validated_recommendations(
    result: Recommendations, enrolled_ids: frozenset[UUID], others_ids: frozenset[UUID]
) -> tuple[Recommendations, int]:
    enrolled, dropped_enrolled = validated_group(result.enrolled, enrolled_ids)
    others, dropped_others = validated_group(result.others, others_ids)
    return Recommendations(enrolled=enrolled, others=others), dropped_enrolled + dropped_others


async def enqueue_project_refresh(
    session: AsyncSession, jobs: procrastinate.App, project_id: UUID
) -> None:
    await defer_job(session, jobs, JobName.REFRESH_RECOMMENDATIONS, project_id=str(project_id))


async def enqueue_event_refresh(
    session: AsyncSession, jobs: procrastinate.App, event_id: UUID
) -> None:
    project_ids = await session.scalars(
        select(models.Project.id)
        .where(
            models.Project.event_id == event_id,
            or_(
                models.Project.team_status.in_(
                    sorted(state.value for state in formation_common.CAPACITY_TEAM_STATES)
                ),
                and_(
                    models.Project.team_status.is_(None),
                    models.Project.recruiting.is_not(None),
                ),
            ),
        )
        .order_by(models.Project.id)
    )
    for project_id in project_ids.all():
        await enqueue_project_refresh(session, jobs, project_id)


async def request_refresh(
    session: AsyncSession,
    jobs: procrastinate.App,
    project_id: UUID,
    actor: models.User,
    now: datetime,
) -> None:
    project = await get_or_404(session, models.Project, project_id, lock=True)
    if actor.id != project.lead_user_id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if not recommendation_refreshable(project):
        raise ProblemError(ErrorCode.INVALID_STATE, "Quick-pick is not available for this project")
    if (
        project.recommendation_refresh_at is not None
        and now - project.recommendation_refresh_at < REFRESH_INTERVAL
    ):
        raise ProblemError(
            ErrorCode.RATE_LIMITED, "Recommendations can be refreshed once per minute"
        )
    project.recommendation_refresh_at = now
    await session.flush()
    await enqueue_project_refresh(session, jobs, project.id)


async def replace_recommendation_set(
    session: AsyncSession,
    *,
    subject_type: RecommendationSubjectType,
    subject_id: UUID,
    input_hash: str,
    algo_version: str,
    others: Sequence[Rec] | None,
    error: str | None,
    now: datetime,
) -> None:
    await session.execute(
        delete(models.RecommendationSet).where(
            models.RecommendationSet.subject_type == subject_type.value,
            models.RecommendationSet.subject_id == subject_id,
        )
    )
    session.add(
        models.RecommendationSet(
            subject_type=subject_type.value,
            subject_id=subject_id,
            input_hash=input_hash,
            status=(RecommendationStatus.FAILED if error else RecommendationStatus.READY).value,
            error=error,
            enrolled=None,
            others=None if others is None else [rec.model_dump(mode="json") for rec in others],
            algo_version=algo_version,
            completed_at=None if error else now,
        )
    )
    await session.flush()


async def list_suggested_mentors(
    session: AsyncSession, actor: models.User, request_id: UUID, now: datetime
) -> list[schemas.Recommendation]:
    request = await session.get(models.HelpRequest, request_id)
    if request is None or not (
        request.status == HelpRequestStatus.OPEN.value
        or actor.id in {request.learner_id, request.target_mentor_id}
    ):
        raise ProblemError(ErrorCode.NOT_FOUND)
    if actor.id != request.learner_id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if request.status not in (HelpRequestStatus.OPEN.value, HelpRequestStatus.DIRECT.value):
        return []
    row = await latest_recommendation_set(
        session, RecommendationSubjectType.HELP_REQUEST, request_id
    )
    if row is None or row.status != RecommendationStatus.READY.value or row.others is None:
        return []
    recs = stored_recs(row.others)
    if not recs:
        return []
    users = {
        user.id: user
        for user in await session.scalars(
            select(models.User).where(models.User.id.in_([rec.user_id for rec in recs]))
        )
    }
    return [
        schemas.Recommendation.model_validate(
            {
                **rec.model_dump(),
                "profile": user_ref(user),
            }
        )
        for rec in recs
        if (user := users.get(rec.user_id)) is not None and eligible_user(user, now)
    ]
