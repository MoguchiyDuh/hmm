from uuid import UUID

from hmm_contracts.algo import OnboardingBaseline as OnboardingBaselineData
from hmm_contracts.common import MemberReviewStatus, SubmissionStatus
from sqlalchemy import select

from hmm_core import models
from hmm_core.onboarding import scoring
from hmm_core.onboarding.baseline import load_onboarding_baseline, save_onboarding_baseline
from hmm_core.platform.algo import call_algo
from hmm_core.platform.jobs import JobName, Runtime, defer_job, job
from hmm_core.scores.service import write_calculated_scores


@job(JobName.SCORE_ONBOARDING)
async def score_onboarding(runtime: Runtime, user_id: str) -> None:
    now = runtime.clock.now()
    async with runtime.sessionmaker.begin() as session:
        user = await session.get(models.User, UUID(user_id))
        if user is None:
            return
        submission = await session.scalar(
            select(models.OnboardingSubmission)
            .where(
                models.OnboardingSubmission.user_id == user.id,
                models.OnboardingSubmission.status == SubmissionStatus.SUBMITTED,
            )
            .order_by(
                models.OnboardingSubmission.submitted_at.desc(),
                models.OnboardingSubmission.created_at.desc(),
                models.OnboardingSubmission.id.desc(),
            )
            .limit(1)
        )
        if submission is None or submission.submitted_at is None:
            return
        existing = await load_onboarding_baseline(session, user.id)
        if existing is not None and existing.submitted_at == submission.submitted_at:
            return
        github = await session.get(models.GithubSnapshot, user.id)
        cv = await session.get(models.CvFile, user.id)
        if not scoring.settled(github, cv):
            remaining = (submission.submitted_at + scoring.WAIT_LIMIT - now).total_seconds()
            if remaining > 0:
                await defer_job(
                    session,
                    runtime.jobs,
                    JobName.SCORE_ONBOARDING,
                    user_id=user_id,
                    schedule_in=min(scoring.POLL_SECONDS, remaining),
                )
                return
        areas = list(
            await session.scalars(
                select(models.Area)
                .where(models.Area.archived_at.is_(None))
                .order_by(models.Area.sort, models.Area.key)
            )
        )
        request = await scoring.build_request(session, submission, areas)
        ctx = await runtime.algo_context(session)
        result = await call_algo(
            "score_onboarding", runtime.algorithm.score_onboarding(ctx, request)
        )
        await save_onboarding_baseline(
            session,
            user.id,
            OnboardingBaselineData(
                submitted_at=submission.submitted_at,
                area_scores=result.area_scores,
                discrepancies=result.discrepancies,
            ),
        )
        await write_calculated_scores(
            session,
            user.id,
            result.area_scores,
            areas=areas,
            algo_version=runtime.algorithm.version,
            now=now,
        )
        review = await session.get(models.MemberReview, user.id, with_for_update=True)
        if review is not None:
            review.discrepancies = list(result.discrepancies)
        else:
            session.add(
                models.MemberReview(
                    user_id=user.id,
                    status=MemberReviewStatus.PENDING,
                    opened_at=now,
                    reviewed_by=None,
                    reviewed_at=None,
                    admin_note=None,
                    discrepancies=list(result.discrepancies),
                )
            )
        await defer_job(session, runtime.jobs, JobName.RECOMPUTE_MEMBER, user_id=user_id)
        await defer_job(
            session, runtime.jobs, JobName.COMPUTE_LEAD_FITS_FOR_MEMBER, user_id=user_id
        )
