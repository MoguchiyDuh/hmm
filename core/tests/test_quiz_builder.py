from copy import deepcopy
from typing import Any
from uuid import UUID, uuid4

import httpx
from harness import Harness
from hmm_contracts.common import ActorKind, AuditAction, AuditEntityType, ErrorCode
from hmm_core import models
from sqlalchemy import func, select, update
from test_api_skeleton import assert_problem

DEFINITION: dict[str, Any] = {
    "title": "Onboarding",
    "sections": [
        {"key": "general", "position": 0, "visible_if": None},
        {
            "key": "advanced",
            "position": 1,
            "visible_if": {
                "any": [
                    {"q": "languages", "op": "includes", "value": "python"},
                    {"q": "years", "op": "gte", "value": 2},
                ]
            },
        },
    ],
    "questions": [
        {
            "id": "019b0000-0000-7000-8000-000000000001",
            "section": "general",
            "key": "languages",
            "type": "multi_choice",
            "text": "Languages",
            "help": None,
            "required": True,
            "position": 0,
            "visible_if": None,
            "config": {"min": 0, "max": 5, "allow_other": True},
            "options": [
                {
                    "id": "019b0000-0000-7000-8000-000000000002",
                    "key": "python",
                    "label": "Python",
                    "position": 0,
                    "area_hints": {"backend": 1.0},
                },
                {
                    "id": "019b0000-0000-7000-8000-000000000003",
                    "key": "react",
                    "label": "React",
                    "position": 1,
                    "area_hints": {"frontend": 0.8},
                },
            ],
        },
        {
            "id": "019b0000-0000-7000-8000-000000000004",
            "section": "general",
            "key": "years",
            "type": "number",
            "text": "Years",
            "help": "Total years",
            "required": False,
            "position": 1,
            "visible_if": None,
            "config": {"min": 0, "max": 50, "unit": "years"},
            "options": [],
        },
        {
            "id": "019b0000-0000-7000-8000-000000000005",
            "section": "general",
            "key": "direction",
            "type": "single_choice",
            "text": "Direction",
            "help": None,
            "required": True,
            "position": 2,
            "visible_if": {"q": "languages", "op": "includes", "value": "python"},
            "config": {"allow_other": False},
            "options": [
                {
                    "id": "019b0000-0000-7000-8000-000000000006",
                    "key": "backend",
                    "label": "Backend",
                    "position": 0,
                    "area_hints": {"backend": 1.0},
                },
                {
                    "id": "019b0000-0000-7000-8000-000000000007",
                    "key": "frontend",
                    "label": "Frontend",
                    "position": 1,
                    "area_hints": {"frontend": 1.0},
                },
            ],
        },
        {
            "id": "019b0000-0000-7000-8000-000000000008",
            "section": "general",
            "key": "rating",
            "type": "scale",
            "text": "Self rating",
            "help": None,
            "required": False,
            "position": 3,
            "visible_if": None,
            "config": {"min": 1, "max": 10, "min_label": "new", "max_label": "expert"},
            "options": [],
        },
        {
            "id": "019b0000-0000-7000-8000-000000000009",
            "section": "advanced",
            "key": "about",
            "type": "text",
            "text": "About",
            "help": None,
            "required": False,
            "position": 4,
            "visible_if": {"not": {"q": "years", "op": "gte", "value": 2}},
            "config": {"max_length": 2000, "multiline": True},
            "options": [],
        },
        {
            "id": "019b0000-0000-7000-8000-000000000010",
            "section": "advanced",
            "key": "link",
            "type": "url",
            "text": "Link",
            "help": None,
            "required": False,
            "position": 5,
            "visible_if": None,
            "config": {},
            "options": [],
        },
    ],
}

QUIZZES = "/api/v1/admin/quizzes"


async def create_quiz(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.post(QUIZZES, json={"title": "Onboarding"})
    assert response.status_code == 201, response.text
    return response.json()


async def tree_counts(h: Harness, quiz_id: str) -> tuple[int, int, int]:
    async with h.db() as session:
        questions = await session.scalar(
            select(func.count()).where(models.Question.quiz_id == UUID(quiz_id))
        )
        options = await session.scalar(
            select(func.count())
            .select_from(models.QuestionOption)
            .join(models.Question, models.Question.id == models.QuestionOption.question_id)
            .where(models.Question.quiz_id == UUID(quiz_id))
        )
        sections = await session.scalar(
            select(func.count()).where(models.QuizSectionDef.quiz_id == UUID(quiz_id))
        )
        assert questions is not None and options is not None and sections is not None
        return questions, options, sections


async def test_create_list_get(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    async with await h.client(admin) as client:
        quiz = await create_quiz(client)
        assert quiz["version"] == 1
        assert quiz["title"] == "Onboarding"
        assert quiz["status"] == "draft"
        assert quiz["published_at"] is None
        assert quiz["created_by"] == str(admin.id)
        assert quiz["sections"] == []
        assert quiz["questions"] == []
        assert quiz["allowed_actions"] == ["edit", "validate", "publish", "clone", "delete"]
        assert quiz["created_at"] and quiz["updated_at"]
        response = await client.get(f"{QUIZZES}/{quiz['id']}")
        assert response.status_code == 200, response.text
        assert response.json() == quiz
        response = await client.get(QUIZZES)
        assert response.status_code == 200, response.text
        assert response.json() == [quiz]
        second = await create_quiz(client)
        assert second["version"] == 2
        response = await client.get(QUIZZES)
        assert response.status_code == 200, response.text
        assert response.json() == [second, quiz]
    async with h.db() as session:
        audit = await session.scalar(
            select(models.AuditLog).where(
                models.AuditLog.action == AuditAction.QUIZ_CREATE,
                models.AuditLog.entity_id == quiz["id"],
            )
        )
        assert audit is not None
        assert audit.actor_id == admin.id
        assert audit.actor_kind == ActorKind.ADMIN
        assert audit.entity_type == AuditEntityType.QUIZ
        assert audit.before is None
        assert audit.after == {"version": 1, "title": "Onboarding"}
    assert await h.pending_jobs() == []


async def test_replace_roundtrip(harness: Harness) -> None:
    h = harness
    await h.seed_areas(("backend", "frontend"))
    admin = await h.admin()
    async with await h.client(admin) as client:
        quiz = await create_quiz(client)
        path = f"{QUIZZES}/{quiz['id']}"
        response = await client.put(path, json=DEFINITION)
        assert response.status_code == 200, response.text
        snapshot = response.json()
        assert snapshot["sections"] == DEFINITION["sections"]
        assert snapshot["questions"] == DEFINITION["questions"]
        response = await client.put(path, json=DEFINITION)
        assert response.status_code == 200, response.text
        assert response.json() == snapshot
        response = await client.get(path)
        assert response.status_code == 200, response.text
        assert response.json() == snapshot
    assert await tree_counts(h, quiz["id"]) == (6, 4, 2)
    async with h.db() as session:
        audits = list(
            await session.scalars(
                select(models.AuditLog)
                .where(models.AuditLog.action == AuditAction.QUIZ_EDIT)
                .order_by(models.AuditLog.created_at)
            )
        )
        assert len(audits) == 2
        assert audits[0].before == {"title": "Onboarding", "sections": 0, "questions": 0}
        assert audits[0].after == {"title": "Onboarding", "sections": 2, "questions": 6}
        assert audits[1].before == audits[0].after
        assert all(audit.actor_id == admin.id and audit.entity_id == quiz["id"] for audit in audits)
    assert await h.pending_jobs() == []


async def test_draft_only(harness: Harness) -> None:
    h = harness
    await h.seed_areas(("backend", "frontend"))
    admin = await h.admin()
    async with await h.client(admin) as client:
        quiz = await create_quiz(client)
        path = f"{QUIZZES}/{quiz['id']}"
        response = await client.put(path, json=DEFINITION)
        assert response.status_code == 200, response.text
        async with h.db() as session:
            await session.execute(
                update(models.Quiz)
                .where(models.Quiz.id == UUID(quiz["id"]))
                .values(status="published")
            )
        assert_problem(await client.put(path, json=DEFINITION), 409, ErrorCode.NOT_DRAFT)
        assert_problem(await client.delete(path), 409, ErrorCode.NOT_DRAFT)
        response = await client.get(path)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "published"
        assert response.json()["allowed_actions"] == ["validate", "clone"]


async def test_failed_replacement_rolls_back(harness: Harness) -> None:
    h = harness
    await h.seed_areas(("backend", "frontend"))
    admin = await h.admin()
    async with await h.client(admin) as client:
        quiz = await create_quiz(client)
        path = f"{QUIZZES}/{quiz['id']}"
        response = await client.put(path, json=DEFINITION)
        assert response.status_code == 200, response.text
        snapshot = response.json()
        invalid = deepcopy(DEFINITION)
        invalid["questions"][1]["key"] = "languages"
        assert_problem(await client.put(path, json=invalid), 422, ErrorCode.VALIDATION_ERROR)
        response = await client.get(path)
        assert response.status_code == 200, response.text
        assert response.json() == snapshot
    assert await tree_counts(h, quiz["id"]) == (6, 4, 2)


async def test_delete_draft(harness: Harness) -> None:
    h = harness
    await h.seed_areas(("backend", "frontend"))
    admin = await h.admin()
    async with await h.client(admin) as client:
        quiz = await create_quiz(client)
        path = f"{QUIZZES}/{quiz['id']}"
        response = await client.put(path, json=DEFINITION)
        assert response.status_code == 200, response.text
        response = await client.request("DELETE", path, json={"reason": "Obsolete draft"})
        assert response.status_code == 204, response.text
        assert response.content == b""
        assert_problem(await client.delete(path), 404, ErrorCode.NOT_FOUND)
    assert await tree_counts(h, quiz["id"]) == (0, 0, 0)
    async with h.db() as session:
        assert await session.get(models.Quiz, UUID(quiz["id"])) is None
        assert await session.scalar(select(func.count()).select_from(models.QuestionOption)) == 0
        audit = await session.scalar(
            select(models.AuditLog).where(models.AuditLog.action == AuditAction.QUIZ_DELETE)
        )
        assert audit is not None
        assert audit.actor_id == admin.id
        assert audit.actor_kind == ActorKind.ADMIN
        assert audit.entity_type == AuditEntityType.QUIZ
        assert audit.entity_id == quiz["id"]
        assert audit.before == {"version": 1, "title": "Onboarding", "status": "draft"}
        assert audit.after is None
        assert audit.reason == "Obsolete draft"


async def test_access_and_foreign_ids(harness: Harness) -> None:
    h = harness
    await h.seed_areas(("backend", "frontend"))
    admin = await h.admin()
    member = await h.user()
    async with await h.client(admin) as client:
        quiz = await create_quiz(client)
        path = f"{QUIZZES}/{quiz['id']}"
        response = await client.put(path, json=DEFINITION)
        assert response.status_code == 200, response.text
        async with await h.client(member) as other:
            assert_problem(await other.get(QUIZZES), 403, ErrorCode.FORBIDDEN)
            assert_problem(await other.get(path), 403, ErrorCode.FORBIDDEN)
            assert_problem(
                await other.post(QUIZZES, json={"title": "Forbidden"}), 403, ErrorCode.FORBIDDEN
            )
            assert_problem(await other.put(path, json=DEFINITION), 403, ErrorCode.FORBIDDEN)
            assert_problem(await other.delete(path), 403, ErrorCode.FORBIDDEN)
        missing = f"{QUIZZES}/{uuid4()}"
        assert_problem(await client.get(missing), 404, ErrorCode.NOT_FOUND)
        assert_problem(await client.put(missing, json=DEFINITION), 404, ErrorCode.NOT_FOUND)
        assert_problem(await client.delete(missing), 404, ErrorCode.NOT_FOUND)
        second = await create_quiz(client)
        second_path = f"{QUIZZES}/{second['id']}"
        response = await client.put(second_path, json=DEFINITION)
        assert_problem(response, 422, ErrorCode.VALIDATION_ERROR)
        assert response.json()["detail"] == "Question id is already used"
        foreign_options = deepcopy(DEFINITION)
        for question in foreign_options["questions"]:
            question["id"] = str(uuid4())
        response = await client.put(second_path, json=foreign_options)
        assert_problem(response, 422, ErrorCode.VALIDATION_ERROR)
        assert response.json()["detail"] == "Option id is already used"
        for duplicate, detail in (
            ("question", "Question id is duplicated"),
            ("option", "Option id is duplicated"),
        ):
            invalid = deepcopy(DEFINITION)
            if duplicate == "question":
                invalid["questions"][1]["id"] = invalid["questions"][0]["id"]
            else:
                invalid["questions"][2]["options"][0]["id"] = invalid["questions"][0]["options"][0][
                    "id"
                ]
            response = await client.put(path, json=invalid)
            assert_problem(response, 422, ErrorCode.VALIDATION_ERROR)
            assert response.json()["detail"] == detail
        response = await client.get(second_path)
        assert response.status_code == 200, response.text
        assert response.json() == second
    assert await tree_counts(h, quiz["id"]) == (6, 4, 2)
    assert await tree_counts(h, second["id"]) == (0, 0, 0)
