from typing import Any
from uuid import UUID

from harness import Harness
from hmm_contracts.common import (
    AuditAction,
    AuditEntityType,
    CvTextStatus,
    ErrorCode,
    OnboardingStatus,
    QuizStatus,
    SubmissionStatus,
)
from hmm_core import models
from sqlalchemy import func, select, update
from test_api_skeleton import assert_problem

ONBOARDING = "/api/v1/onboarding"
ANSWERS = "/api/v1/onboarding/answers"
SUBMIT = "/api/v1/onboarding/submit"

QUESTIONS: list[dict[str, Any]] = [
    {
        "key": "languages",
        "type": "multi_choice",
        "section": "general",
        "required": True,
        "visible_if": None,
        "config": {"min": 1, "max": 2, "allow_other": True},
        "options": [("python", {"backend": 1.0}), ("react", {"frontend": 0.8})],
    },
    {
        "key": "years",
        "type": "number",
        "section": "general",
        "required": False,
        "visible_if": None,
        "config": {"min": 0, "max": 50, "unit": "years"},
        "options": [],
    },
    {
        "key": "direction",
        "type": "single_choice",
        "section": "general",
        "required": True,
        "visible_if": {"q": "languages", "op": "includes", "value": "python"},
        "config": {"allow_other": False},
        "options": [("backend", {"backend": 1.0}), ("frontend", {"frontend": 1.0})],
    },
    {
        "key": "about",
        "type": "text",
        "section": "advanced",
        "required": False,
        "visible_if": {"not": {"q": "years", "op": "gte", "value": 2}},
        "config": {"max_length": 20, "multiline": False},
        "options": [],
    },
    {
        "key": "rating",
        "type": "scale",
        "section": "general",
        "required": False,
        "visible_if": None,
        "config": {"min": 1, "max": 10, "min_label": "new", "max_label": "expert"},
        "options": [],
    },
    {
        "key": "link",
        "type": "url",
        "section": "general",
        "required": False,
        "visible_if": None,
        "config": {},
        "options": [],
    },
]


async def _archive_published(session: Any) -> None:
    await session.execute(
        update(models.Quiz)
        .where(models.Quiz.status == QuizStatus.PUBLISHED)
        .values(status=QuizStatus.ARCHIVED)
    )
    await session.flush()


async def _next_version(session: Any) -> int:
    return (await session.scalar(select(func.coalesce(func.max(models.Quiz.version), 0)))) + 1


async def seed_quiz(h: Harness, admin: models.User, *, drop: str | None = None) -> models.Quiz:
    async with h.db() as session:
        await _archive_published(session)
        quiz = models.Quiz(
            version=await _next_version(session),
            title="Onboarding",
            status=QuizStatus.PUBLISHED,
            created_by=admin.id,
            published_at=h.now,
        )
        session.add(quiz)
        await session.flush()
        session.add_all(
            [
                models.QuizSectionDef(quiz_id=quiz.id, key="general", position=0, visible_if=None),
                models.QuizSectionDef(
                    quiz_id=quiz.id,
                    key="advanced",
                    position=1,
                    visible_if={
                        "any": [
                            {"q": "languages", "op": "includes", "value": "python"},
                            {"q": "years", "op": "gte", "value": 2},
                        ]
                    },
                ),
            ]
        )
        rows: dict[str, models.Question] = {}
        for position, spec in enumerate(QUESTIONS):
            if spec["key"] == drop:
                continue
            rows[spec["key"]] = models.Question(
                quiz_id=quiz.id,
                section=spec["section"],
                key=spec["key"],
                type=spec["type"],
                text=spec["key"].title(),
                help=None,
                required=spec["required"],
                position=position,
                visible_if=spec["visible_if"],
                config=spec["config"],
            )
        session.add_all(rows.values())
        await session.flush()
        session.add_all(
            models.QuestionOption(
                question_id=rows[spec["key"]].id,
                key=key,
                label=key.title(),
                position=index,
                area_hints=hints,
            )
            for spec in QUESTIONS
            if spec["key"] in rows
            for index, (key, hints) in enumerate(spec["options"])
        )
        await session.flush()
        await session.refresh(quiz)
        return quiz


async def _setup(h: Harness) -> tuple[models.User, models.Quiz, models.User]:
    await h.seed_areas(("backend", "frontend"))
    admin = await h.admin()
    quiz = await seed_quiz(h, admin)
    member = await h.user(onboarded=False)
    return admin, quiz, member


async def _submissions(h: Harness, user_id: UUID) -> list[models.OnboardingSubmission]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.OnboardingSubmission)
                .where(models.OnboardingSubmission.user_id == user_id)
                .order_by(models.OnboardingSubmission.created_at)
            )
        )


async def _status(h: Harness, user_id: UUID) -> str:
    async with h.db() as session:
        user = await session.get(models.User, user_id)
        assert user is not None
        return user.onboarding_status


def _answer(value: Any, other: str | None = None) -> dict[str, Any]:
    return {"value": value, "other": other}


async def test_before_publication(harness: Harness) -> None:
    member = await harness.user(onboarded=False)
    async with await harness.client(member) as client:
        response = await client.get(ONBOARDING)
        assert response.status_code == 200, response.text
        assert response.json() == {
            "status": "not_started",
            "quiz": None,
            "draft_answers": {},
            "cv": {"present": False, "text_status": None, "text_error": None},
            "allowed_actions": [],
        }
        response = await client.put(ANSWERS, json={"answers": {"years": {"value": 1}}})
        assert_problem(response, 409, ErrorCode.INVALID_STATE)
        assert_problem(await client.post(SUBMIT), 409, ErrorCode.INVALID_STATE)
    assert await _status(harness, member.id) == OnboardingStatus.NOT_STARTED
    assert await _submissions(harness, member.id) == []
    assert await harness.pending_jobs() == []


async def test_owner_sees_cv_text_failure(harness: Harness) -> None:
    member = await harness.user(onboarded=False)
    async with harness.db() as session:
        session.add(
            models.CvFile(
                user_id=member.id,
                object_key=f"cv/{member.id}",
                filename="cv.pdf",
                mime="application/pdf",
                size_bytes=10,
                uploaded_at=harness.now,
                text=None,
                text_status=CvTextStatus.FAILED,
                text_error="bad pdf",
            )
        )
    async with await harness.client(member) as client:
        response = await client.get(ONBOARDING)
    assert response.status_code == 200, response.text
    assert response.json()["cv"] == {
        "present": True,
        "text_status": "failed",
        "text_error": "bad pdf",
    }


async def test_first_save_and_merge(harness: Harness) -> None:
    _, quiz, member = await _setup(harness)
    async with await harness.client(member) as client:
        response = await client.put(ANSWERS, json={"answers": {"languages": {"value": ["python"]}}})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "in_progress"
        assert body["draft_answers"] == {"languages": _answer(["python"])}
        assert body["allowed_actions"] == ["answer", "submit"]
        assert body["quiz"]["id"] == str(quiz.id)
        assert body["quiz"]["allowed_actions"] == []
        response = await client.put(ANSWERS, json={"answers": {"years": {"value": 3}}})
        assert response.status_code == 200, response.text
        merged = {"languages": _answer(["python"]), "years": _answer(3)}
        assert response.json()["draft_answers"] == merged
        response = await client.put(ANSWERS, json={"answers": {"years": {"value": 3}}})
        assert response.status_code == 200, response.text
        assert response.json()["draft_answers"] == merged
    rows = await _submissions(harness, member.id)
    assert len(rows) == 1
    assert rows[0].status == SubmissionStatus.DRAFT
    assert rows[0].quiz_id == quiz.id


async def test_validation_errors_keep_draft(harness: Harness) -> None:
    _, _, member = await _setup(harness)
    async with await harness.client(member) as client:
        response = await client.put(ANSWERS, json={"answers": {"languages": {"value": ["python"]}}})
        assert response.status_code == 200, response.text
        response = await client.put(ANSWERS, json={"answers": {"rating": {"value": 11}}})
        problem = assert_problem(response, 422, ErrorCode.VALIDATION_ERROR)
        assert problem.detail == "Answers are invalid"
        assert response.json()["errors"] == [
            {"loc": ["answers", "rating", "value"], "msg": "Value is above the maximum"}
        ]
        response = await client.put(
            ANSWERS,
            json={
                "answers": {
                    "link": {"value": "not a url"},
                    "languages": {"value": ["rust"]},
                }
            },
        )
        assert_problem(response, 422, ErrorCode.VALIDATION_ERROR)
        assert response.json()["errors"] == [
            {"loc": ["answers", "languages", "value"], "msg": "Option rust does not exist"},
            {"loc": ["answers", "link", "value"], "msg": "Value must be a URL"},
        ]
    rows = await _submissions(harness, member.id)
    assert len(rows) == 1
    assert rows[0].answers == {"languages": _answer(["python"])}
    assert await _status(harness, member.id) == OnboardingStatus.IN_PROGRESS


async def test_unknown_keys_dropped(harness: Harness) -> None:
    _, _, member = await _setup(harness)
    async with await harness.client(member) as client:
        response = await client.put(
            ANSWERS, json={"answers": {"nope": {"value": 1}, "years": {"value": 2}}}
        )
    assert response.status_code == 200, response.text
    assert response.json()["draft_answers"] == {"years": _answer(2)}
    rows = await _submissions(harness, member.id)
    assert rows[0].answers == {"years": _answer(2)}


async def test_draft_migration_on_load_and_save(harness: Harness) -> None:
    admin, _, member = await _setup(harness)
    async with await harness.client(member) as client:
        response = await client.put(
            ANSWERS,
            json={
                "answers": {
                    "languages": {"value": ["python"]},
                    "link": {"value": "https://example.com"},
                }
            },
        )
        assert response.status_code == 200, response.text
        second = await seed_quiz(harness, admin, drop="link")
        response = await client.get(ONBOARDING)
        assert response.status_code == 200, response.text
        assert response.json()["draft_answers"] == {"languages": _answer(["python"])}
        assert response.json()["quiz"]["id"] == str(second.id)
        rows = await _submissions(harness, member.id)
        assert len(rows) == 1
        assert rows[0].quiz_id == second.id
        assert rows[0].answers == {"languages": _answer(["python"])}
        response = await client.put(ANSWERS, json={"answers": {"years": {"value": 1}}})
        assert response.status_code == 200, response.text
        assert response.json()["draft_answers"] == {
            "languages": _answer(["python"]),
            "years": _answer(1),
        }
    assert len(await _submissions(harness, member.id)) == 1


async def test_submit_success(harness: Harness) -> None:
    _, quiz, member = await _setup(harness)
    answers = {
        "languages": {"value": ["python"]},
        "years": {"value": 1},
        "direction": {"value": "backend"},
        "rating": {"value": 7},
    }
    expected = {key: _answer(item["value"]) for key, item in answers.items()}
    async with await harness.client(member) as client:
        response = await client.put(ANSWERS, json={"answers": answers})
        assert response.status_code == 200, response.text
        response = await client.post(SUBMIT)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"
    assert body["allowed_actions"] == []
    assert body["draft_answers"] == expected
    rows = await _submissions(harness, member.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.status == SubmissionStatus.SUBMITTED
    assert row.answers == expected
    assert row.quiz_id == quiz.id
    assert row.submitted_at == harness.now
    assert await _status(harness, member.id) == OnboardingStatus.COMPLETED
    async with harness.db() as session:
        review = await session.get(models.MemberReview, member.id)
        assert review is not None
        assert review.status == "pending"
        assert review.opened_at == harness.now
        assert review.discrepancies == []
        audits = list(
            await session.scalars(
                select(models.AuditLog).where(
                    models.AuditLog.action == AuditAction.SYSTEM_TRANSITION,
                    models.AuditLog.entity_type == AuditEntityType.ONBOARDING,
                )
            )
        )
    assert len(audits) == 1
    assert isinstance(audits[0].after, dict)
    assert audits[0].after["submission_id"] == str(row.id)
    jobs = await harness.pending_jobs("score_onboarding")
    assert len(jobs) == 1
    assert jobs[0]["args"] == {"user_id": str(member.id)}


async def test_submit_drops_hidden_answers(harness: Harness) -> None:
    _, _, member = await _setup(harness)
    async with await harness.client(member) as client:
        response = await client.put(
            ANSWERS,
            json={
                "answers": {
                    "languages": {"value": ["react"]},
                    "years": {"value": 3},
                    "direction": {"value": "backend"},
                    "about": {"value": "old"},
                }
            },
        )
        assert response.status_code == 200, response.text
        response = await client.post(SUBMIT)
    assert response.status_code == 200, response.text
    expected = {"languages": _answer(["react"]), "years": _answer(3)}
    assert response.json()["draft_answers"] == expected
    rows = await _submissions(harness, member.id)
    assert len(rows) == 1
    assert rows[0].answers == expected


async def test_submit_missing_required(harness: Harness) -> None:
    _, _, member = await _setup(harness)
    async with await harness.client(member) as client:
        response = await client.put(
            ANSWERS,
            json={"answers": {"languages": {"value": ["python"]}, "years": {"value": 1}}},
        )
        assert response.status_code == 200, response.text
        response = await client.post(SUBMIT)
    assert_problem(response, 422, ErrorCode.VALIDATION_ERROR)
    assert response.json()["errors"] == [
        {"loc": ["answers", "direction"], "msg": "Required question is not answered"}
    ]
    assert await _status(harness, member.id) == OnboardingStatus.IN_PROGRESS
    rows = await _submissions(harness, member.id)
    assert [row.status for row in rows] == [SubmissionStatus.DRAFT]
    assert await harness.pending_jobs("score_onboarding") == []
    async with harness.db() as session:
        assert await session.get(models.MemberReview, member.id) is None


async def test_submit_hidden_required_not_required(harness: Harness) -> None:
    _, _, member = await _setup(harness)
    async with await harness.client(member) as client:
        response = await client.put(
            ANSWERS,
            json={"answers": {"languages": {"value": ["react"]}, "years": {"value": 3}}},
        )
        assert response.status_code == 200, response.text
        response = await client.post(SUBMIT)
    assert response.status_code == 200, response.text
    rows = await _submissions(harness, member.id)
    assert isinstance(rows[0].answers, dict)
    assert "direction" not in rows[0].answers


async def test_submit_without_any_draft(harness: Harness) -> None:
    admin = await harness.admin()
    async with harness.db() as session:
        quiz = models.Quiz(
            version=await _next_version(session),
            title="Onboarding",
            status=QuizStatus.PUBLISHED,
            created_by=admin.id,
            published_at=harness.now,
        )
        session.add(quiz)
        await session.flush()
        session.add(
            models.Question(
                quiz_id=quiz.id,
                section="general",
                key="years",
                type="number",
                text="Years",
                help=None,
                required=False,
                position=0,
                visible_if=None,
                config={"min": 0, "max": 50, "unit": "years"},
            )
        )
    member = await harness.user(onboarded=False)
    async with await harness.client(member) as client:
        response = await client.post(SUBMIT)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"
    assert response.json()["draft_answers"] == {}
    rows = await _submissions(harness, member.id)
    assert len(rows) == 1
    assert rows[0].status == SubmissionStatus.SUBMITTED
    assert rows[0].answers == {}
    assert len(await harness.pending_jobs("score_onboarding")) == 1
    assert await _status(harness, member.id) == OnboardingStatus.COMPLETED


async def test_access_and_edit_lock(harness: Harness) -> None:
    _, _, member = await _setup(harness)
    async with await harness.client(member) as client:
        assert_problem(await client.get("/api/v1/areas"), 403, ErrorCode.ONBOARDING_REQUIRED)
        response = await client.put(
            ANSWERS,
            json={"answers": {"languages": {"value": ["react"]}}},
        )
        assert response.status_code == 200, response.text
        response = await client.post(SUBMIT)
        assert response.status_code == 200, response.text
        response = await client.get("/api/v1/areas")
        assert response.status_code == 200, response.text
        response = await client.put(ANSWERS, json={"answers": {"years": {"value": 1}}})
        assert_problem(response, 409, ErrorCode.INVALID_STATE)
        assert_problem(await client.post(SUBMIT), 409, ErrorCode.INVALID_STATE)
    assert len(await harness.pending_jobs("score_onboarding")) == 1


async def test_repeated_submit_is_isolated(harness: Harness) -> None:
    _, quiz, first = await _setup(harness)
    second = await harness.user(onboarded=False)
    for member in (first, second):
        async with await harness.client(member) as client:
            response = await client.put(
                ANSWERS, json={"answers": {"languages": {"value": ["react"]}}}
            )
            assert response.status_code == 200, response.text
            response = await client.post(SUBMIT)
            assert response.status_code == 200, response.text
    jobs = await harness.pending_jobs("score_onboarding")
    assert sorted(job["args"]["user_id"] for job in jobs) == sorted([str(first.id), str(second.id)])
    for member in (first, second):
        rows = await _submissions(harness, member.id)
        assert [(row.status, row.quiz_id) for row in rows] == [
            (SubmissionStatus.SUBMITTED, quiz.id)
        ]
