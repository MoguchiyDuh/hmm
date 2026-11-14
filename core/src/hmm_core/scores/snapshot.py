from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from hmm_contracts.algo import EffectiveScore, MemberSnapshot, MentorOffer
from hmm_contracts.common import (
    ErrorCode,
    MemberReviewStatus,
    MemberRole,
    MemberState,
    MentorshipStatus,
    OverrideEvidence,
    ProjectStatus,
    TeamStatus,
)
from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.identity.service import ONBOARDED
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.settings import load_settings
from hmm_core.profiles.github import github_summary
from hmm_core.scores.reputation import reputation_summaries


async def build_member_snapshot(
    session: AsyncSession, user_id: UUID, now: datetime
) -> MemberSnapshot:
    snapshots = await build_member_snapshots(session, [user_id], now)
    if user_id not in snapshots:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return snapshots[user_id]


async def build_member_snapshots(
    session: AsyncSession, user_ids: Sequence[UUID], now: datetime
) -> dict[UUID, MemberSnapshot]:
    ids = list(dict.fromkeys(user_ids))
    users = {
        user.id: user
        for user in await session.scalars(select(models.User).where(models.User.id.in_(ids)))
        if user.onboarding_status in ONBOARDED
    }
    if not users:
        return {}
    ids = list(users)
    settings = await load_settings(session)
    cvs = {
        row.user_id: row.text
        for row in await session.scalars(
            select(models.CvFile).where(models.CvFile.user_id.in_(ids))
        )
    }
    github = {
        row.user_id: row
        for row in await session.scalars(
            select(models.GithubSnapshot).where(models.GithubSnapshot.user_id.in_(ids))
        )
    }
    scores: dict[UUID, dict[str, EffectiveScore]] = {user_id: {} for user_id in ids}
    score_rows = await session.execute(
        select(models.AreaScore, models.Area)
        .join(models.Area, models.Area.id == models.AreaScore.area_id)
        .where(models.AreaScore.user_id.in_(ids), models.Area.archived_at.is_(None))
        .order_by(models.Area.sort, models.Area.key)
    )
    for row, area in score_rows:
        scores[row.user_id][area.key] = EffectiveScore(
            calculated=float(row.calculated),
            override=float(row.override) if row.override is not None else None,
            override_evidence=OverrideEvidence(row.override_evidence)
            if row.override_evidence is not None
            else None,
            effective=float(row.override if row.override is not None else row.calculated),
        )
    reviews = {
        row.user_id: row.reviewed_at
        for row in await session.scalars(
            select(models.MemberReview).where(
                models.MemberReview.user_id.in_(ids),
                models.MemberReview.status == MemberReviewStatus.REVIEWED,
            )
        )
    }
    metrics = {
        row.user_id: row
        for row in await session.scalars(
            select(models.MemberMetric).where(models.MemberMetric.user_id.in_(ids))
        )
    }
    adjustments = {
        user_id: float(amount)
        for user_id, amount in await session.execute(
            select(models.XpAdjustment.user_id, func.sum(models.XpAdjustment.amount))
            .where(models.XpAdjustment.user_id.in_(ids))
            .group_by(models.XpAdjustment.user_id)
        )
    }
    projects = {
        user_id: (finished, led, shipped)
        for user_id, finished, led, shipped in await session.execute(
            select(
                models.ProjectMember.user_id,
                func.count(distinct(models.ProjectMember.project_id)).filter(
                    models.ProjectMember.state == MemberState.CONFIRMED,
                    models.Project.team_status == TeamStatus.FINISHED,
                ),
                func.count(distinct(models.ProjectMember.project_id)).filter(
                    models.ProjectMember.state == MemberState.CONFIRMED,
                    models.ProjectMember.role == MemberRole.LEAD,
                ),
                func.count(distinct(models.ProjectMember.project_id)).filter(
                    models.ProjectMember.state == MemberState.CONFIRMED,
                    models.Project.event_id.is_(None),
                    models.Project.status == ProjectStatus.SHIPPED,
                ),
            )
            .join(models.Project, models.Project.id == models.ProjectMember.project_id)
            .where(models.ProjectMember.user_id.in_(ids))
            .group_by(models.ProjectMember.user_id)
        )
    }
    mentor_counts = {
        user_id: (closed, active)
        for user_id, closed, active in await session.execute(
            select(
                models.Mentorship.mentor_id,
                func.count().filter(models.Mentorship.status == MentorshipStatus.CLOSED),
                func.count().filter(models.Mentorship.status == MentorshipStatus.ACTIVE),
            )
            .where(models.Mentorship.mentor_id.in_(ids))
            .group_by(models.Mentorship.mentor_id)
        )
    }
    learner_counts = {
        user_id: count
        for user_id, count in await session.execute(
            select(models.Mentorship.learner_id, func.count())
            .where(
                models.Mentorship.learner_id.in_(ids),
                models.Mentorship.status == MentorshipStatus.CLOSED,
            )
            .group_by(models.Mentorship.learner_id)
        )
    }
    offers: dict[UUID, list[MentorOffer]] = {user_id: [] for user_id in ids}
    offer_rows = await session.execute(
        select(models.MentorOffer, models.Area)
        .join(models.Area, models.Area.id == models.MentorOffer.area_id)
        .where(models.MentorOffer.user_id.in_(ids), models.Area.archived_at.is_(None))
        .order_by(models.Area.sort, models.Area.key)
    )
    for row, area in offer_rows:
        offers[row.user_id].append(MentorOffer(area=area.key, topics=row.topics, active=row.active))
    reputations = await reputation_summaries(session, ids)
    snapshots: dict[UUID, MemberSnapshot] = {}
    for user_id, user in users.items():
        metric = metrics.get(user_id)
        finished, led, shipped = projects.get(user_id, (0, 0, 0))
        closed, active = mentor_counts.get(user_id, (0, 0))
        snapshots[user_id] = MemberSnapshot(
            user_id=user_id,
            display_name=user.display_name,
            about_md=user.about_md,
            cv_text=cvs.get(user_id),
            github=github_summary(github.get(user_id)),
            area_scores=scores[user_id],
            scores_reviewed_at=reviews.get(user_id),
            xp=(float(metric.xp) if metric is not None else 0.0) + adjustments.get(user_id, 0.0),
            activity=float(metric.activity) if metric is not None else 0.0,
            hackathons_finished=finished,
            times_led=led,
            projects_shipped=shipped,
            mentorships_completed=closed + learner_counts.get(user_id, 0),
            reputation=reputations[user_id],
            mentor_offers=offers[user_id],
            mentor_capacity=user.mentor_capacity
            if user.mentor_capacity is not None
            else settings.mentor_capacity_default,
            mentor_active=active,
            joined_at=user.created_at,
        )
    return snapshots
