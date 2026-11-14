from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
from harness import Harness
from hmm_contracts.common import (
    AreaScoreEventKind,
    AuditAction,
    AuditEntityType,
    ErrorCode,
    MemberReviewStatus,
    OnboardingStatus,
    OverrideEvidence,
    QuizStatus,
    SubmissionStatus,
)
from hmm_core import models
from sqlalchemy import func, select, update
from test_api_skeleton import assert_problem

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

FIRST = {"languages": ["python"], "direction": "backend", "years": 1}
SECOND = {"languages": ["react"], "direction": "frontend", "years": 3}


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


def _payload(values: dict[str, Any]) -> dict[str, Any]:
    return {"answers": {key: {"value": value} for key, value in values.items()}}


def _answer(value: Any) -> dict[str, Any]:
    return {"value": value, "other": None}


async def save(client: httpx.AsyncClient, values: dict[str, Any]) -> httpx.Response:
    response = await client.put(ANSWERS, json=_payload(values))
    assert response.status_code == 200, response.text
    return response


async def submit(client: httpx.AsyncClient, values: dict[str, Any]) -> httpx.Response:
    await save(client, values)
    return await client.post(SUBMIT)


def _reopen_url(user_id: UUID) -> str:
    return f"/api/v1/admin/users/{user_id}/onboarding/reopen"


def _history_url(user_id: UUID) -> str:
    return f"/api/v1/admin/users/{user_id}/onboarding"


async def _submissions(h: Harness, user_id: UUID) -> list[models.OnboardingSubmission]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.OnboardingSubmission)
                .where(models.OnboardingSubmission.user_id == user_id)
                .order_by(models.OnboardingSubmission.created_at, models.OnboardingSubmission.id)
            )
        )


async def _status(h: Harness, user_id: UUID) -> str:
    async with h.db() as session:
        user = await session.get(models.User, user_id)
        assert user is not None
        return user.onboarding_status


async def _notifications(h: Harness, user_id: UUID) -> list[models.Notification]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.Notification).where(models.Notification.user_id == user_id)
            )
        )


async def _reopen_audits(h: Harness) -> list[models.AuditLog]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.AuditLog).where(
                    models.AuditLog.action == AuditAction.ONBOARDING_REOPEN
                )
            )
        )


async def test_history_is_immutable(harness: Harness) -> None:
    admin, _, member = await _setup(harness)
    async with await harness.client(member) as client:
        response = await submit(client, FIRST)
        assert response.status_code == 200, response.text
    first = await _submissions(harness, member.id)
    assert len(first) == 1
    frozen = (first[0].id, first[0].answers, first[0].quiz_id, first[0].submitted_at)
    async with await harness.client(admin) as client:
        response = await client.post(_reopen_url(member.id))
        assert response.status_code == 200, response.text
    async with await harness.client(member) as client:
        response = await submit(client, SECOND)
        assert response.status_code == 200, response.text
    async with await harness.client(admin) as client:
        response = await client.get(_history_url(member.id))
    assert response.status_code == 200, response.text
    history = response.json()
    assert len(history) == 2
    assert history[0]["id"] == str(frozen[0])
    assert history[0]["answers"]["direction"] == _answer("backend")
    assert history[0]["quiz"]["version"] == 1
    assert "direction" not in history[1]["answers"]
    assert history[1]["answers"] == {"languages": _answer(["react"]), "years": _answer(3)}
    assert [entry["status"] for entry in history] == ["submitted", "submitted"]
    rows = await _submissions(harness, member.id)
    assert [row.status for row in rows] == [SubmissionStatus.SUBMITTED] * 2
    old = next(row for row in rows if row.id == frozen[0])
    assert (old.id, old.answers, old.quiz_id, old.submitted_at) == frozen


async def test_reopen_prefills_current_version(harness: Harness) -> None:
    admin, _, member = await _setup(harness)
    async with await harness.client(member) as client:
        response = await submit(client, {**FIRST, "link": "https://example.com"})
        assert response.status_code == 200, response.text
    second = await seed_quiz(harness, admin, drop="link")
    async with await harness.client(admin) as client:
        response = await client.post(_reopen_url(member.id))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "reopened"
    assert body["quiz"]["version"] == 2
    assert body["draft_answers"] == {
        "languages": _answer(["python"]),
        "direction": _answer("backend"),
        "years": _answer(1),
    }
    assert "link" not in body["draft_answers"]
    assert body["allowed_actions"] == []
    drafts = [
        row
        for row in await _submissions(harness, member.id)
        if row.status == SubmissionStatus.DRAFT
    ]
    assert len(drafts) == 1
    assert drafts[0].quiz_id == second.id
    assert await _status(harness, member.id) == OnboardingStatus.REOPENED


async def test_reopen_notification_and_audit(harness: Harness) -> None:
    admin, _, member = await _setup(harness)
    await harness.link_telegram(member)
    async with await harness.client(member) as client:
        response = await submit(client, FIRST)
        assert response.status_code == 200, response.text
    async with await harness.client(admin) as client:
        response = await client.post(_reopen_url(member.id), json={"reason": "Fix ML answers"})
        assert response.status_code == 200, response.text
        notifications = await _notifications(harness, member.id)
        assert len(notifications) == 1
        notification = notifications[0]
        assert notification.type == "onboarding_reopened"
        assert isinstance(notification.payload, dict)
        assert notification.payload["actor"] == {
            "user_id": str(admin.id),
            "display_name": admin.display_name,
        }
        assert notification.payload["reason"] == "Fix ML answers"
        assert notification.payload["link"] == "/onboarding"
        audits = await _reopen_audits(harness)
        assert len(audits) == 1
        assert audits[0].entity_type == AuditEntityType.ONBOARDING
        assert str(audits[0].entity_id) == str(member.id)
        assert audits[0].reason == "Fix ML answers"
        response = await client.post(_reopen_url(member.id), json={"reason": "Again"})
        assert_problem(response, 409, ErrorCode.INVALID_STATE)
    assert len(await _notifications(harness, member.id)) == 1
    assert len(await _reopen_audits(harness)) == 1


async def test_reopen_preserves_overrides_and_access(harness: Harness) -> None:
    admin, _, member = await _setup(harness)
    async with await harness.client(member) as client:
        response = await submit(client, FIRST)
        assert response.status_code == 200, response.text
    async with harness.db() as session:
        area = await session.scalar(select(models.Area).where(models.Area.key == "backend"))
        assert area is not None
        session.add(
            models.AreaScore(
                user_id=member.id,
                area_id=area.id,
                calculated=Decimal("3.00"),
                calc_reason="seed",
                override=Decimal("8.00"),
                override_by=admin.id,
                override_reason="interview",
                override_evidence=OverrideEvidence.INTERVIEW,
                override_at=harness.now,
            )
        )
        session.add(
            models.AreaScoreEvent(
                user_id=member.id,
                area_id=area.id,
                kind=AreaScoreEventKind.OVERRIDE_SET,
                old_effective=Decimal("3.00"),
                new_effective=Decimal("8.00"),
                reason="interview",
                algo_version=None,
                actor_id=admin.id,
            )
        )
        session.add(
            models.OnboardingBaseline(
                user_id=member.id,
                submitted_at=harness.now,
                area_scores={"backend": 3.0},
                discrepancies=[],
            )
        )
        area_id = area.id
    async with await harness.client(admin) as client:
        response = await client.post(_reopen_url(member.id))
        assert response.status_code == 200, response.text
    async with harness.db() as session:
        score = await session.get(models.AreaScore, (member.id, area_id))
        assert score is not None
        assert score.calculated == Decimal("3.00")
        assert score.override == Decimal("8.00")
        assert score.override_by == admin.id
        assert score.override_reason == "interview"
        assert score.override_evidence == OverrideEvidence.INTERVIEW
        assert score.override_at == harness.now
        events = await session.scalar(
            select(func.count()).where(models.AreaScoreEvent.user_id == member.id)
        )
        assert events == 1
        baseline = await session.get(models.OnboardingBaseline, member.id)
        assert baseline is not None
        assert baseline.submitted_at == harness.now
        assert baseline.area_scores == {"backend": 3.0}
        assert baseline.discrepancies == []
    async with await harness.client(member) as client:
        response = await client.get("/api/v1/areas")
    assert response.status_code == 200, response.text


async def test_resubmit_reopens_review(harness: Harness) -> None:
    admin, _, member = await _setup(harness)
    async with await harness.client(member) as client:
        response = await submit(client, FIRST)
        assert response.status_code == 200, response.text
    async with harness.db() as session:
        review = await session.get(models.MemberReview, member.id)
        assert review is not None
        review.status = MemberReviewStatus.REVIEWED
        review.reviewed_by = admin.id
        review.reviewed_at = harness.now
        review.admin_note = "looks fine"
    async with await harness.client(admin) as client:
        response = await client.post(_reopen_url(member.id))
        assert response.status_code == 200, response.text
    async with await harness.client(member) as client:
        response = await submit(client, SECOND)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"
    async with harness.db() as session:
        review = await session.get(models.MemberReview, member.id)
        assert review is not None
        assert review.status == MemberReviewStatus.PENDING
        assert review.opened_at == harness.now
        assert review.reviewed_by is None
        assert review.reviewed_at is None
        assert review.admin_note == "looks fine"
        assert review.discrepancies == []
    assert len(await harness.pending_jobs("score_onboarding")) == 1
    rows = await _submissions(harness, member.id)
    assert [row.status for row in rows] == [SubmissionStatus.SUBMITTED] * 2


async def test_reopen_errors(harness: Harness) -> None:
    admin, _, fresh = await _setup(harness)
    started = await harness.user(onboarded=False)
    done = await harness.user(onboarded=False)
    async with await harness.client(started) as client:
        await save(client, {"years": 1})
    async with await harness.client(done) as client:
        response = await submit(client, FIRST)
        assert response.status_code == 200, response.text
    async with await harness.client(admin) as client:
        assert_problem(await client.post(_reopen_url(fresh.id)), 409, ErrorCode.INVALID_STATE)
        assert_problem(await client.post(_reopen_url(started.id)), 409, ErrorCode.INVALID_STATE)
        assert_problem(await client.post(_reopen_url(uuid4())), 404, ErrorCode.NOT_FOUND)
    async with await harness.client(done) as client:
        assert_problem(await client.post(_reopen_url(done.id)), 403, ErrorCode.FORBIDDEN)
    async with harness.db() as session:
        await _archive_published(session)
    async with await harness.client(admin) as client:
        assert_problem(await client.post(_reopen_url(done.id)), 409, ErrorCode.INVALID_STATE)
    assert await _status(harness, fresh.id) == OnboardingStatus.NOT_STARTED
    assert await _status(harness, started.id) == OnboardingStatus.IN_PROGRESS
    assert await _status(harness, done.id) == OnboardingStatus.COMPLETED
    for user in (fresh, started, done):
        assert await _notifications(harness, user.id) == []
    assert await _reopen_audits(harness) == []


async def test_history_access_and_ordering(harness: Harness) -> None:
    admin, _, member = await _setup(harness)
    drafting = await harness.user(onboarded=False)
    async with await harness.client(drafting) as client:
        await save(client, {"years": 1})
    async with await harness.client(admin) as client:
        response = await client.get(_history_url(member.id))
        assert response.status_code == 200, response.text
        assert response.json() == []
        assert_problem(await client.get(_history_url(uuid4())), 404, ErrorCode.NOT_FOUND)
        response = await client.get(_history_url(drafting.id))
        assert response.status_code == 200, response.text
        assert response.json() == []
    async with await harness.client(member) as client:
        assert_problem(await client.get(_history_url(member.id)), 403, ErrorCode.FORBIDDEN)
