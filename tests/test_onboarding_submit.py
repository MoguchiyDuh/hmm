from typing import Any
from uuid import UUID

from factories import (
    next_quiz_version,
    onboarding_setup,
    onboarding_status,
    seed_quiz,
)
from harness import Harness, assert_problem
from sqlalchemy import select

from hmm import models
from hmm.common import (
    AuditAction,
    AuditEntityType,
    CvTextStatus,
    ErrorCode,
    OnboardingStatus,
    QuizStatus,
    SubmissionStatus,
)

ONBOARDING = "/api/v1/onboarding"
ANSWERS = "/api/v1/onboarding/answers"
SUBMIT = "/api/v1/onboarding/submit"


async def _submissions(h: Harness, user_id: UUID) -> list[models.OnboardingSubmission]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.OnboardingSubmission)
                .where(models.OnboardingSubmission.user_id == user_id)
                .order_by(models.OnboardingSubmission.created_at)
            )
        )


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
    assert await onboarding_status(harness, member.id) == OnboardingStatus.NOT_STARTED
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
    _, quiz, member = await onboarding_setup(harness)
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
    _, _, member = await onboarding_setup(harness)
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
    assert await onboarding_status(harness, member.id) == OnboardingStatus.IN_PROGRESS


async def test_unknown_keys_dropped(harness: Harness) -> None:
    _, _, member = await onboarding_setup(harness)
    async with await harness.client(member) as client:
        response = await client.put(
            ANSWERS, json={"answers": {"nope": {"value": 1}, "years": {"value": 2}}}
        )
    assert response.status_code == 200, response.text
    assert response.json()["draft_answers"] == {"years": _answer(2)}
    rows = await _submissions(harness, member.id)
    assert rows[0].answers == {"years": _answer(2)}


async def test_draft_migration_on_load_and_save(harness: Harness) -> None:
    admin, _, member = await onboarding_setup(harness)
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
    _, quiz, member = await onboarding_setup(harness)
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
    assert await onboarding_status(harness, member.id) == OnboardingStatus.COMPLETED
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
    _, _, member = await onboarding_setup(harness)
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
    _, _, member = await onboarding_setup(harness)
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
    assert await onboarding_status(harness, member.id) == OnboardingStatus.IN_PROGRESS
    rows = await _submissions(harness, member.id)
    assert [row.status for row in rows] == [SubmissionStatus.DRAFT]
    assert await harness.pending_jobs("score_onboarding") == []
    async with harness.db() as session:
        assert await session.get(models.MemberReview, member.id) is None


async def test_submit_hidden_required_not_required(harness: Harness) -> None:
    _, _, member = await onboarding_setup(harness)
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
            version=await next_quiz_version(session),
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
    assert await onboarding_status(harness, member.id) == OnboardingStatus.COMPLETED


async def test_access_and_edit_lock(harness: Harness) -> None:
    _, _, member = await onboarding_setup(harness)
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
    _, quiz, first = await onboarding_setup(harness)
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
