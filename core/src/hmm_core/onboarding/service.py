from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

import procrastinate
from hmm_contracts.algo import OnboardingAnswer, QuizQuestion, QuizSection
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
from hmm_contracts.notifications import (
    NotificationPerson,
    OnboardingReopenedPayload,
    ScoreChange,
    ScoresReviewedPayload,
)
from pydantic import JsonValue, TypeAdapter
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.identity.service import lock_user
from hmm_core.notifications.service import notify
from hmm_core.onboarding import schemas
from hmm_core.onboarding.answers import known_answers, submission_answers, validate_values
from hmm_core.onboarding.schemas import (
    MemberReviewAction,
    OnboardingAction,
    OnboardingCv,
    QuizAction,
    UpdateAnswersBody,
    UpdateQuizBody,
)
from hmm_core.onboarding.validation import validate_definition
from hmm_core.platform.audit import record_audit
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.jobs import JobName, defer_job
from hmm_core.platform.pagination import CursorParams, keyset_page
from hmm_core.platform.schemas import UserRef
from hmm_core.platform.storage import ObjectStore
from hmm_core.profiles import service as profiles
from hmm_core.profiles.github import github_summary
from hmm_core.scores import service as scores_service

QUIZ_LOCK = "SELECT pg_advisory_xact_lock(hashtext('quizzes'))"
QUESTION_LIST: TypeAdapter[list[QuizQuestion]] = TypeAdapter(list[QuizQuestion])
SECTION_LIST: TypeAdapter[list[QuizSection]] = TypeAdapter(list[QuizSection])


async def lock_quizzes(session: AsyncSession) -> None:
    await session.execute(text(QUIZ_LOCK))


async def next_version(session: AsyncSession) -> int:
    return await session.scalar(select(func.coalesce(func.max(models.Quiz.version), 0) + 1)) or 1


async def require_quiz(
    session: AsyncSession, quiz_id: UUID, *, for_update: bool = False
) -> models.Quiz:
    statement = select(models.Quiz).where(models.Quiz.id == quiz_id)
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    quiz = await session.scalar(statement)
    if quiz is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return quiz


def quiz_actions(quiz: models.Quiz) -> list[QuizAction]:
    allowed = {QuizAction.VALIDATE, QuizAction.CLONE}
    if quiz.status == QuizStatus.DRAFT:
        allowed |= {QuizAction.EDIT, QuizAction.PUBLISH, QuizAction.DELETE}
    return [action for action in QuizAction if action in allowed]


async def tree_rows(
    session: AsyncSession, quiz_id: UUID
) -> tuple[
    list[models.QuizSectionDef],
    list[models.Question],
    dict[UUID, list[models.QuestionOption]],
]:
    sections = list(
        await session.scalars(
            select(models.QuizSectionDef)
            .where(models.QuizSectionDef.quiz_id == quiz_id)
            .order_by(models.QuizSectionDef.position, models.QuizSectionDef.key)
        )
    )
    rows = await session.execute(
        select(models.Question, models.QuestionOption)
        .outerjoin(models.QuestionOption, models.QuestionOption.question_id == models.Question.id)
        .where(models.Question.quiz_id == quiz_id)
        .order_by(
            models.Question.position,
            models.Question.key,
            models.QuestionOption.position,
            models.QuestionOption.key,
        )
    )
    questions: list[models.Question] = []
    options: dict[UUID, list[models.QuestionOption]] = {}
    for question, option in rows:
        if question.id not in options:
            questions.append(question)
            options[question.id] = []
        if option is not None:
            options[question.id].append(option)
    return sections, questions, options


async def definition(
    session: AsyncSession, quiz_id: UUID
) -> tuple[list[QuizSection], list[QuizQuestion]]:
    sections, questions, options = await tree_rows(session, quiz_id)
    return (
        [
            QuizSection.model_validate(
                {"key": row.key, "position": row.position, "visible_if": row.visible_if}
            )
            for row in sections
        ],
        QUESTION_LIST.validate_python(
            [
                {
                    "id": row.id,
                    "section": row.section,
                    "key": row.key,
                    "type": row.type,
                    "text": row.text,
                    "help": row.help,
                    "required": row.required,
                    "position": row.position,
                    "visible_if": row.visible_if,
                    "config": row.config,
                    "options": [
                        {
                            "id": option.id,
                            "key": option.key,
                            "label": option.label,
                            "position": option.position,
                            "area_hints": option.area_hints,
                        }
                        for option in options[row.id]
                    ],
                }
                for row in questions
            ]
        ),
    )


async def quiz_view(session: AsyncSession, quiz: models.Quiz) -> schemas.Quiz:
    sections, questions = await definition(session, quiz.id)
    return schemas.Quiz(
        id=quiz.id,
        version=quiz.version,
        title=quiz.title,
        status=QuizStatus(quiz.status),
        published_at=quiz.published_at,
        created_by=quiz.created_by,
        created_at=quiz.created_at,
        updated_at=quiz.updated_at,
        allowed_actions=quiz_actions(quiz),
        sections=sections,
        questions=questions,
    )


async def list_quizzes(session: AsyncSession) -> list[schemas.Quiz]:
    quizzes = await session.scalars(select(models.Quiz).order_by(models.Quiz.version.desc()))
    return [await quiz_view(session, quiz) for quiz in quizzes]


async def create_quiz(
    session: AsyncSession, actor: models.User, title: str, reason: str | None
) -> schemas.Quiz:
    await lock_quizzes(session)
    version = await next_version(session)
    quiz = models.Quiz(version=version, title=title, status=QuizStatus.DRAFT, created_by=actor.id)
    session.add(quiz)
    await session.flush()
    await session.refresh(quiz)
    await record_audit(
        session,
        actor=actor.id,
        actor_kind=ActorKind.ADMIN,
        action=AuditAction.QUIZ_CREATE,
        entity_type=AuditEntityType.QUIZ,
        entity_id=quiz.id,
        before=None,
        after={"version": version, "title": title},
        reason=reason,
    )
    return await quiz_view(session, quiz)


async def get_quiz(session: AsyncSession, quiz_id: UUID) -> schemas.Quiz:
    return await quiz_view(session, await require_quiz(session, quiz_id))


async def replace_quiz(
    session: AsyncSession, actor: models.User, quiz_id: UUID, body: UpdateQuizBody
) -> schemas.Quiz:
    await lock_quizzes(session)
    quiz = await require_quiz(session, quiz_id, for_update=True)
    if quiz.status != QuizStatus.DRAFT:
        raise ProblemError(ErrorCode.NOT_DRAFT)
    areas = list(
        await session.scalars(select(models.Area).order_by(models.Area.sort, models.Area.key))
    )
    result = validate_definition(body.sections, body.questions, areas)
    if result.errors:
        raise ProblemError(ErrorCode.VALIDATION_ERROR, "Quiz definition has validation errors")
    question_ids = [question.id for question in body.questions]
    option_ids = [option.id for question in body.questions for option in question.options]
    if len(set(question_ids)) != len(question_ids):
        raise ProblemError(ErrorCode.VALIDATION_ERROR, "Question id is duplicated")
    if len(set(option_ids)) != len(option_ids):
        raise ProblemError(ErrorCode.VALIDATION_ERROR, "Option id is duplicated")
    if (
        await session.scalar(
            select(models.Question.id).where(
                models.Question.id.in_(question_ids), models.Question.quiz_id != quiz.id
            )
        )
        is not None
    ):
        raise ProblemError(ErrorCode.VALIDATION_ERROR, "Question id is already used")
    if (
        await session.scalar(
            select(models.QuestionOption.id)
            .join(models.Question, models.Question.id == models.QuestionOption.question_id)
            .where(models.QuestionOption.id.in_(option_ids), models.Question.quiz_id != quiz.id)
        )
        is not None
    ):
        raise ProblemError(ErrorCode.VALIDATION_ERROR, "Option id is already used")
    old_title = quiz.title
    old_question_count = await session.scalar(
        select(func.count()).where(models.Question.quiz_id == quiz.id)
    )
    old_section_count = await session.scalar(
        select(func.count()).where(models.QuizSectionDef.quiz_id == quiz.id)
    )
    await session.execute(delete(models.Question).where(models.Question.quiz_id == quiz.id))
    await session.execute(
        delete(models.QuizSectionDef).where(models.QuizSectionDef.quiz_id == quiz.id)
    )
    await session.flush()
    session.add_all(
        models.QuizSectionDef(
            quiz_id=quiz.id,
            key=section.key,
            position=section.position,
            visible_if=section.visible_if.model_dump(mode="json", by_alias=True)
            if section.visible_if is not None
            else None,
        )
        for section in body.sections
    )
    session.add_all(
        models.Question(
            id=question.id,
            quiz_id=quiz.id,
            section=question.section,
            key=question.key,
            type=question.type.value,
            text=question.text,
            help=question.help,
            required=question.required,
            position=question.position,
            visible_if=question.visible_if.model_dump(mode="json", by_alias=True)
            if question.visible_if is not None
            else None,
            config=question.config.model_dump(mode="json", by_alias=True),
        )
        for question in body.questions
    )
    await session.flush()
    session.add_all(
        models.QuestionOption(
            id=option.id,
            question_id=question.id,
            key=option.key,
            label=option.label,
            position=option.position,
            area_hints=dict(option.area_hints),
        )
        for question in body.questions
        for option in question.options
    )
    quiz.title = body.title
    await session.flush()
    await session.refresh(quiz)
    await record_audit(
        session,
        actor=actor.id,
        actor_kind=ActorKind.ADMIN,
        action=AuditAction.QUIZ_EDIT,
        entity_type=AuditEntityType.QUIZ,
        entity_id=quiz.id,
        before={"title": old_title, "sections": old_section_count, "questions": old_question_count},
        after={
            "title": body.title,
            "sections": len(body.sections),
            "questions": len(body.questions),
        },
        reason=body.reason,
    )
    return await quiz_view(session, quiz)


async def delete_quiz(
    session: AsyncSession, actor: models.User, quiz_id: UUID, reason: str | None
) -> None:
    await lock_quizzes(session)
    quiz = await require_quiz(session, quiz_id, for_update=True)
    if quiz.status != QuizStatus.DRAFT:
        raise ProblemError(ErrorCode.NOT_DRAFT)
    await record_audit(
        session,
        actor=actor.id,
        actor_kind=ActorKind.ADMIN,
        action=AuditAction.QUIZ_DELETE,
        entity_type=AuditEntityType.QUIZ,
        entity_id=quiz.id,
        before={"version": quiz.version, "title": quiz.title, "status": quiz.status},
        after=None,
        reason=reason,
    )
    await session.execute(delete(models.Quiz).where(models.Quiz.id == quiz.id))


async def clone_quiz(
    session: AsyncSession, actor: models.User, quiz_id: UUID, reason: str | None
) -> schemas.Quiz:
    await lock_quizzes(session)
    source = await require_quiz(session, quiz_id, for_update=True)
    sections, questions, options = await tree_rows(session, source.id)
    clone = models.Quiz(
        version=await next_version(session),
        title=f"{source.title[:195]} copy",
        status=QuizStatus.DRAFT,
        created_by=actor.id,
    )
    session.add(clone)
    await session.flush()
    session.add_all(
        models.QuizSectionDef(
            quiz_id=clone.id,
            key=section.key,
            position=section.position,
            visible_if=section.visible_if,
        )
        for section in sections
    )
    new_questions = [
        models.Question(
            quiz_id=clone.id,
            section=question.section,
            key=question.key,
            type=question.type,
            text=question.text,
            help=question.help,
            required=question.required,
            position=question.position,
            visible_if=question.visible_if,
            config=question.config,
        )
        for question in questions
    ]
    session.add_all(new_questions)
    await session.flush()
    for new_question, source_question in zip(new_questions, questions, strict=True):
        for option in options[source_question.id]:
            assert isinstance(option.area_hints, dict)
            session.add(
                models.QuestionOption(
                    question_id=new_question.id,
                    key=option.key,
                    label=option.label,
                    position=option.position,
                    area_hints=dict(option.area_hints),
                )
            )
    await session.flush()
    await record_audit(
        session,
        actor=actor.id,
        actor_kind=ActorKind.ADMIN,
        action=AuditAction.QUIZ_CLONE,
        entity_type=AuditEntityType.QUIZ,
        entity_id=clone.id,
        before={"source_id": str(source.id), "source_version": source.version},
        after={"id": str(clone.id), "version": clone.version, "title": clone.title},
        reason=reason,
    )
    return await quiz_view(session, clone)


async def validate_quiz(
    session: AsyncSession, actor: models.User, quiz_id: UUID, reason: str | None
) -> schemas.QuizValidation:
    quiz = await require_quiz(session, quiz_id)
    sections, questions = await definition(session, quiz_id)
    areas = list(
        await session.scalars(select(models.Area).order_by(models.Area.sort, models.Area.key))
    )
    result = validate_definition(sections, questions, areas)
    await record_audit(
        session,
        actor=actor.id,
        actor_kind=ActorKind.ADMIN,
        action=AuditAction.QUIZ_VALIDATE,
        entity_type=AuditEntityType.QUIZ,
        entity_id=quiz.id,
        before=None,
        after={"errors": len(result.errors), "warnings": len(result.warnings)},
        reason=reason,
    )
    return result


async def publish_quiz(
    session: AsyncSession,
    actor: models.User,
    quiz_id: UUID,
    reason: str | None,
    now: datetime,
) -> schemas.Quiz:
    await lock_quizzes(session)
    quiz = await require_quiz(session, quiz_id, for_update=True)
    if quiz.status != QuizStatus.DRAFT:
        raise ProblemError(ErrorCode.NOT_DRAFT)
    sections, questions = await definition(session, quiz_id)
    areas = list(
        await session.scalars(select(models.Area).order_by(models.Area.sort, models.Area.key))
    )
    result = validate_definition(sections, questions, areas)
    if result.errors:
        raise ProblemError(ErrorCode.VALIDATION_ERROR, "Quiz definition has validation errors")
    previous = await session.scalar(
        select(models.Quiz).where(models.Quiz.status == QuizStatus.PUBLISHED).with_for_update()
    )
    if previous is not None:
        previous.status = QuizStatus.ARCHIVED
        await session.flush()
    quiz.status = QuizStatus.PUBLISHED
    quiz.published_at = now
    await session.flush()
    await session.refresh(quiz)
    await record_audit(
        session,
        actor=actor.id,
        actor_kind=ActorKind.ADMIN,
        action=AuditAction.QUIZ_PUBLISH,
        entity_type=AuditEntityType.QUIZ,
        entity_id=quiz.id,
        before={"status": "draft"},
        after={
            "status": "published",
            "published_at": now.isoformat(),
            "archived_id": str(previous.id) if previous else None,
            "archived_version": previous.version if previous else None,
        },
        reason=reason,
    )
    return await quiz_view(session, quiz)


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


def review_actions(review: models.MemberReview) -> list[schemas.MemberReviewAction]:
    actions = [MemberReviewAction.EDIT]
    if review.status == MemberReviewStatus.PENDING:
        actions.append(MemberReviewAction.COMPLETE)
    return actions


async def require_review(
    session: AsyncSession, user_id: UUID, *, for_update: bool = False
) -> models.MemberReview:
    statement = select(models.MemberReview).where(models.MemberReview.user_id == user_id)
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    review = await session.scalar(statement)
    if review is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return review


async def member_review_view(
    session: AsyncSession, review: models.MemberReview
) -> schemas.MemberReview:
    user = await session.get(models.User, review.user_id)
    if user is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return schemas.MemberReview(
        user=UserRef(id=user.id, display_name=user.display_name, avatar_url=user.avatar_url),
        status=MemberReviewStatus(review.status),
        opened_at=review.opened_at,
        reviewed_by=review.reviewed_by,
        reviewed_at=review.reviewed_at,
        admin_note=review.admin_note,
        discrepancies=[str(item) for item in review.discrepancies]
        if isinstance(review.discrepancies, list)
        else [],
        allowed_actions=review_actions(review),
    )


async def member_reviews_page(
    session: AsyncSession, pagination: CursorParams, status: MemberReviewStatus | None
) -> schemas.MemberReviewPage:
    statement = select(models.MemberReview)
    if status is not None:
        statement = statement.where(models.MemberReview.status == status.value)
    page = await keyset_page(
        session,
        statement,
        pagination,
        sort=[models.MemberReview.opened_at],
        id_column=models.MemberReview.user_id,
        serialize=lambda row: row,
        descending=False,
    )
    return schemas.MemberReviewPage(
        items=[await member_review_view(session, review) for review in page.items],
        next_cursor=page.next_cursor,
    )


async def member_review_detail(
    session: AsyncSession, store: ObjectStore | None, user_id: UUID, actor: models.User
) -> schemas.MemberReviewDetail:
    review = await require_review(session, user_id)
    base = await member_review_view(session, review)
    submissions = await user_onboarding_history(session, user_id)
    cv: schemas.ReviewCv | None = None
    if await session.get(models.CvFile, user_id) is not None:
        row, url = await profiles.cv_download(
            session, profiles.require_storage(store), actor=actor, user_id=user_id
        )
        cv = schemas.ReviewCv(
            filename=row.filename,
            url=url,
            text=row.text,
            text_status=CvTextStatus(row.text_status),
            text_error=row.text_error,
        )
    github = github_summary(await session.get(models.GithubSnapshot, user_id))
    area_scores = await scores_service.list_scores(session, user_id, actor)
    return schemas.MemberReviewDetail(
        user=base.user,
        status=base.status,
        opened_at=base.opened_at,
        reviewed_by=base.reviewed_by,
        reviewed_at=base.reviewed_at,
        admin_note=base.admin_note,
        discrepancies=base.discrepancies,
        allowed_actions=base.allowed_actions,
        submissions=submissions,
        cv=cv,
        github=github,
        area_scores=area_scores,
    )


async def update_member_review(
    session: AsyncSession, actor: models.User, user_id: UUID, note: str, reason: str | None
) -> schemas.MemberReview:
    review = await require_review(session, user_id, for_update=True)
    if review.admin_note == note:
        return await member_review_view(session, review)
    before: dict[str, JsonValue] = {"admin_note": review.admin_note}
    review.admin_note = note
    await session.flush()
    await record_audit(
        session,
        actor=actor.id,
        actor_kind=ActorKind.ADMIN,
        action=AuditAction.MEMBER_REVIEW_EDIT,
        entity_type=AuditEntityType.MEMBER_REVIEW,
        entity_id=user_id,
        before=before,
        after={"admin_note": note},
        reason=reason,
    )
    return await member_review_view(session, review)


async def complete_member_review(
    session: AsyncSession,
    actor: models.User,
    user_id: UUID,
    note: str | None,
    reason: str | None,
    now: datetime,
) -> schemas.MemberReview:
    review = await require_review(session, user_id, for_update=True)
    if review.status == MemberReviewStatus.REVIEWED:
        return await member_review_view(session, review)
    before: dict[str, JsonValue] = {
        "status": "pending",
        "reviewed_by": str(review.reviewed_by) if review.reviewed_by is not None else None,
        "reviewed_at": review.reviewed_at.isoformat() if review.reviewed_at is not None else None,
        "admin_note": review.admin_note,
    }
    review.status = MemberReviewStatus.REVIEWED
    review.reviewed_by = actor.id
    review.reviewed_at = now
    if note is not None:
        review.admin_note = note
    await session.flush()
    await record_audit(
        session,
        actor=actor.id,
        actor_kind=ActorKind.ADMIN,
        action=AuditAction.MEMBER_REVIEW_COMPLETE,
        entity_type=AuditEntityType.MEMBER_REVIEW,
        entity_id=user_id,
        before=before,
        after={
            "status": "reviewed",
            "reviewed_by": str(actor.id),
            "reviewed_at": now.isoformat(),
            "admin_note": review.admin_note,
        },
        reason=reason,
    )
    rows = await session.execute(
        select(models.AreaScore, models.Area)
        .join(models.Area, models.Area.id == models.AreaScore.area_id)
        .where(models.AreaScore.user_id == user_id, models.Area.archived_at.is_(None))
        .order_by(models.Area.sort)
    )
    changes = tuple(
        ScoreChange(
            area_key=area.key,
            area_name=area.name,
            previous_score=float(score.calculated),
            score=float(score.override),
            reason=score.override_reason or score.calc_reason,
        )
        for score, area in rows
        if score.override is not None and score.override != score.calculated
    )
    await notify(
        session,
        user_id,
        ScoresReviewedPayload(
            link=f"/users/{user_id}",
            reviewer=NotificationPerson(user_id=actor.id, display_name=actor.display_name),
            changes=changes,
        ),
        now,
    )
    return await member_review_view(session, review)
