from collections.abc import Sequence
from datetime import timedelta

from hmm_contracts.algo import Area as AlgoArea
from hmm_contracts.algo import (
    OnboardingAnswer,
    OnboardingAnswers,
    OnboardingRequest,
    QuizDef,
)
from hmm_contracts.common import CvTextStatus, GithubSnapshotStatus, QuizStatus
from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.onboarding.service import definition
from hmm_core.profiles.github import github_summary
from hmm_core.profiles.service import RegistrationInputs, inputs_settled

WAIT_LIMIT = timedelta(minutes=10)
POLL_SECONDS = 15.0
ANSWERS: TypeAdapter[dict[str, OnboardingAnswer]] = TypeAdapter(dict[str, OnboardingAnswer])


def settled(github: models.GithubSnapshot | None, cv: models.CvFile | None) -> bool:
    return inputs_settled(
        RegistrationInputs(
            github=GithubSnapshotStatus(github.status) if github else None,
            cv=CvTextStatus(cv.text_status) if cv else None,
        )
    )


def cv_text(row: models.CvFile | None) -> str | None:
    if row is None or row.text_status != CvTextStatus.OK.value:
        return None
    return row.text


async def build_request(
    session: AsyncSession, submission: models.OnboardingSubmission, areas: Sequence[models.Area]
) -> OnboardingRequest:
    quiz = await session.get(models.Quiz, submission.quiz_id)
    if quiz is None:
        raise RuntimeError("Onboarding quiz is missing")
    sections, questions = await definition(session, quiz.id)
    return OnboardingRequest(
        quiz=QuizDef(
            id=quiz.id,
            version=quiz.version,
            title=quiz.title,
            status=QuizStatus(quiz.status),
            published_at=quiz.published_at,
            sections=sections,
            questions=questions,
        ),
        answers=OnboardingAnswers(answers=ANSWERS.validate_python(submission.answers)),
        cv_text=cv_text(await session.get(models.CvFile, submission.user_id)),
        github=github_summary(await session.get(models.GithubSnapshot, submission.user_id)),
        areas=[
            AlgoArea(
                id=area.id,
                key=area.key,
                name=area.name,
                description=area.description,
                sort=area.sort,
                archived_at=area.archived_at,
            )
            for area in areas
        ],
    )
