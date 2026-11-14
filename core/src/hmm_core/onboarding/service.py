from datetime import datetime
from uuid import UUID

from hmm_contracts.algo import QuizQuestion, QuizSection
from hmm_contracts.common import ActorKind, AuditAction, AuditEntityType, ErrorCode, QuizStatus
from pydantic import TypeAdapter
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.onboarding import schemas
from hmm_core.onboarding.schemas import QuizAction, UpdateQuizBody
from hmm_core.onboarding.validation import validate_definition
from hmm_core.platform.audit import record_audit
from hmm_core.platform.errors import ProblemError

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
