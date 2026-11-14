from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

import procrastinate
from hmm_contracts.algo import OnboardingAnswer, QuizQuestion
from hmm_contracts.common import (
    ActorKind,
    AuditAction,
    AuditEntityType,
    CvTextStatus,
    ErrorCode,
    MemberReviewStatus,
    OnboardingStatus,
    QuizStatus,
    SubmissionStatus,
)
from hmm_contracts.notifications import NotificationPerson, OnboardingReopenedPayload
from pydantic import JsonValue, TypeAdapter
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.identity.service import lock_user
from hmm_core.notifications.service import notify
from hmm_core.onboarding import schemas
from hmm_core.onboarding.answers import known_answers, submission_answers, validate_values
from hmm_core.onboarding.quizzes import definition, quiz_view
from hmm_core.onboarding.schemas import OnboardingAction, OnboardingCv, UpdateAnswersBody
from hmm_core.platform.audit import record_audit
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.jobs import JobName, defer_job

EDITABLE = frozenset(
    {OnboardingStatus.NOT_STARTED, OnboardingStatus.IN_PROGRESS, OnboardingStatus.REOPENED}
)
ANSWER_MAP: TypeAdapter[dict[str, OnboardingAnswer]] = TypeAdapter(dict[str, OnboardingAnswer])


async def published_quiz(session: AsyncSession) -> models.Quiz | None:
    return await session.scalar(
        select(models.Quiz).where(models.Quiz.status == QuizStatus.PUBLISHED)
    )


async def require_published_quiz(session: AsyncSession) -> models.Quiz:
    quiz = await published_quiz(session)
    if quiz is None:
        raise ProblemError(ErrorCode.INVALID_STATE, "No published quiz")
    return quiz


async def live_draft(
    session: AsyncSession, user_id: UUID, *, for_update: bool = False
) -> models.OnboardingSubmission | None:
    statement = select(models.OnboardingSubmission).where(
        models.OnboardingSubmission.user_id == user_id,
        models.OnboardingSubmission.status == SubmissionStatus.DRAFT,
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return await session.scalar(statement)


async def latest_submitted(
    session: AsyncSession, user_id: UUID
) -> models.OnboardingSubmission | None:
    return await session.scalar(
        select(models.OnboardingSubmission)
        .where(
            models.OnboardingSubmission.user_id == user_id,
            models.OnboardingSubmission.status == SubmissionStatus.SUBMITTED,
        )
        .order_by(
            models.OnboardingSubmission.submitted_at.desc(),
            models.OnboardingSubmission.created_at.desc(),
            models.OnboardingSubmission.id.desc(),
        )
        .limit(1)
    )


def dump_answers(answers: Mapping[str, OnboardingAnswer]) -> dict[str, JsonValue]:
    return {key: answer.model_dump(mode="json") for key, answer in answers.items()}


async def migrate_draft(
    session: AsyncSession, draft: models.OnboardingSubmission, quiz: models.Quiz
) -> None:
    if draft.quiz_id == quiz.id:
        return
    _, questions = await definition(session, quiz.id)
    draft.quiz_id = quiz.id
    draft.answers = dump_answers(
        known_answers(questions, ANSWER_MAP.validate_python(draft.answers))
    )


async def onboarding_view(
    session: AsyncSession,
    user: models.User,
    quiz: models.Quiz | None,
    *,
    viewer: models.User,
) -> schemas.Onboarding:
    quiz_field: schemas.Quiz | None = None
    questions: list[QuizQuestion] = []
    if quiz is not None:
        quiz_field = (await quiz_view(session, quiz)).model_copy(update={"allowed_actions": []})
        _, questions = await definition(session, quiz.id)
    answers: dict[str, OnboardingAnswer] = {}
    draft = await live_draft(session, user.id)
    if draft is not None:
        answers = ANSWER_MAP.validate_python(draft.answers)
    else:
        submitted = await latest_submitted(session, user.id)
        if submitted is not None:
            answers = known_answers(questions, ANSWER_MAP.validate_python(submitted.answers))
    cv_row = await session.get(models.CvFile, user.id)
    sees_text = cv_row is not None and (viewer.id == user.id or viewer.is_admin)
    cv = OnboardingCv(
        present=cv_row is not None,
        text_status=CvTextStatus(cv_row.text_status) if cv_row is not None and sees_text else None,
        text_error=cv_row.text_error if cv_row is not None and sees_text else None,
    )
    allowed: set[OnboardingAction] = set()
    if viewer.id == user.id and user.onboarding_status in EDITABLE and quiz is not None:
        allowed = {OnboardingAction.ANSWER, OnboardingAction.SUBMIT}
    return schemas.Onboarding(
        status=OnboardingStatus(user.onboarding_status),
        quiz=quiz_field,
        draft_answers=answers,
        cv=cv,
        allowed_actions=[action for action in OnboardingAction if action in allowed],
    )


async def get_onboarding(session: AsyncSession, actor: models.User) -> schemas.Onboarding:
    quiz = await published_quiz(session)
    if quiz is not None:
        draft = await live_draft(session, actor.id, for_update=True)
        if draft is not None:
            await migrate_draft(session, draft, quiz)
    return await onboarding_view(session, actor, quiz, viewer=actor)


async def _ensure_draft(
    session: AsyncSession, actor: models.User, quiz: models.Quiz
) -> models.OnboardingSubmission:
    draft = await live_draft(session, actor.id, for_update=True)
    if draft is None:
        draft = models.OnboardingSubmission(
            user_id=actor.id, quiz_id=quiz.id, answers={}, status=SubmissionStatus.DRAFT
        )
        session.add(draft)
        await session.flush()
        return draft
    await migrate_draft(session, draft, quiz)
    return draft


async def update_answers(
    session: AsyncSession, actor: models.User, body: UpdateAnswersBody
) -> schemas.Onboarding:
    actor = await lock_user(session, actor.id)
    if actor.onboarding_status not in EDITABLE:
        raise ProblemError(ErrorCode.INVALID_STATE, "Onboarding is not editable")
    quiz = await require_published_quiz(session)
    _, questions = await definition(session, quiz.id)
    keys = {question.key for question in questions}
    draft = await _ensure_draft(session, actor, quiz)
    provided = {key: answer for key, answer in body.answers.items() if key in keys}
    issues = validate_values(questions, provided)
    if issues:
        raise ProblemError(ErrorCode.VALIDATION_ERROR, "Answers are invalid", issues)
    draft.answers = dump_answers({**ANSWER_MAP.validate_python(draft.answers), **provided})
    if actor.onboarding_status == OnboardingStatus.NOT_STARTED:
        actor.onboarding_status = OnboardingStatus.IN_PROGRESS
    await session.flush()
    return await onboarding_view(session, actor, quiz, viewer=actor)


async def submit_onboarding(
    session: AsyncSession, jobs: procrastinate.App, actor: models.User, now: datetime
) -> schemas.Onboarding:
    actor = await lock_user(session, actor.id)
    if actor.onboarding_status not in EDITABLE:
        raise ProblemError(ErrorCode.INVALID_STATE, "Onboarding is already submitted")
    quiz = await require_published_quiz(session)
    sections, questions = await definition(session, quiz.id)
    draft = await _ensure_draft(session, actor, quiz)
    answers, issues = submission_answers(
        sections, questions, ANSWER_MAP.validate_python(draft.answers)
    )
    if issues:
        raise ProblemError(ErrorCode.VALIDATION_ERROR, "Answers are invalid", issues)
    draft.answers = dump_answers(answers)
    draft.quiz_id = quiz.id
    draft.status = SubmissionStatus.SUBMITTED
    draft.submitted_at = now
    before = OnboardingStatus(actor.onboarding_status)
    actor.onboarding_status = OnboardingStatus.COMPLETED
    review = await session.get(models.MemberReview, actor.id, with_for_update=True)
    if review is None:
        session.add(
            models.MemberReview(
                user_id=actor.id,
                status=MemberReviewStatus.PENDING,
                opened_at=now,
                reviewed_by=None,
                reviewed_at=None,
                admin_note=None,
                discrepancies=[],
            )
        )
    else:
        review.status = MemberReviewStatus.PENDING
        review.opened_at = now
        review.reviewed_by = None
        review.reviewed_at = None
        review.discrepancies = []
    await record_audit(
        session,
        actor=actor.id,
        actor_kind=ActorKind.USER,
        action=AuditAction.SYSTEM_TRANSITION,
        entity_type=AuditEntityType.ONBOARDING,
        entity_id=actor.id,
        before={"status": before.value},
        after={
            "status": "completed",
            "quiz_id": str(quiz.id),
            "submission_id": str(draft.id),
            "answers": len(answers),
        },
        reason=None,
    )
    await defer_job(session, jobs, JobName.SCORE_ONBOARDING, user_id=str(actor.id))
    return await onboarding_view(session, actor, quiz, viewer=actor)


async def user_onboarding_history(
    session: AsyncSession, user_id: UUID
) -> list[schemas.OnboardingSubmission]:
    target = await session.get(models.User, user_id)
    if target is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    rows = await session.scalars(
        select(models.OnboardingSubmission)
        .where(
            models.OnboardingSubmission.user_id == user_id,
            models.OnboardingSubmission.status == SubmissionStatus.SUBMITTED,
        )
        .order_by(
            models.OnboardingSubmission.submitted_at,
            models.OnboardingSubmission.created_at,
            models.OnboardingSubmission.id,
        )
    )
    history: list[schemas.OnboardingSubmission] = []
    for row in rows.all():
        quiz = await session.get(models.Quiz, row.quiz_id)
        assert quiz is not None
        history.append(
            schemas.OnboardingSubmission(
                id=row.id,
                user_id=row.user_id,
                quiz_id=row.quiz_id,
                quiz=await quiz_view(session, quiz),
                answers=ANSWER_MAP.validate_python(row.answers),
                status=SubmissionStatus(row.status),
                submitted_at=row.submitted_at,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
        )
    return history


async def reopen_onboarding(
    session: AsyncSession,
    user_id: UUID,
    actor: models.User,
    reason: str | None,
    now: datetime,
) -> schemas.Onboarding:
    target = await lock_user(session, user_id)
    if target.onboarding_status == OnboardingStatus.REOPENED:
        raise ProblemError(ErrorCode.INVALID_STATE, "Onboarding is already reopened")
    if target.onboarding_status != OnboardingStatus.COMPLETED:
        raise ProblemError(ErrorCode.INVALID_STATE, "Onboarding is not submitted")
    quiz = await require_published_quiz(session)
    submission = await latest_submitted(session, user_id)
    if submission is None:
        raise ProblemError(ErrorCode.INVALID_STATE, "No submitted onboarding")
    _, questions = await definition(session, quiz.id)
    answers = known_answers(questions, ANSWER_MAP.validate_python(submission.answers))
    await session.execute(
        delete(models.OnboardingSubmission).where(
            models.OnboardingSubmission.user_id == user_id,
            models.OnboardingSubmission.status == SubmissionStatus.DRAFT,
        )
    )
    session.add(
        models.OnboardingSubmission(
            user_id=user_id,
            quiz_id=quiz.id,
            answers=dump_answers(answers),
            status=SubmissionStatus.DRAFT,
        )
    )
    await session.flush()
    target.onboarding_status = OnboardingStatus.REOPENED
    await record_audit(
        session,
        actor=actor.id,
        actor_kind=ActorKind.ADMIN,
        action=AuditAction.ONBOARDING_REOPEN,
        entity_type=AuditEntityType.ONBOARDING,
        entity_id=user_id,
        before={"status": "completed"},
        after={
            "status": "reopened",
            "quiz_id": str(quiz.id),
            "source_submission_id": str(submission.id),
        },
        reason=reason,
    )
    await notify(
        session,
        user_id,
        OnboardingReopenedPayload(
            link="/onboarding",
            actor=NotificationPerson(user_id=actor.id, display_name=actor.display_name),
            reason=reason,
        ),
        now,
    )
    return await onboarding_view(session, target, quiz, viewer=actor)
