from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from harness import Harness
from hmm_contracts.algo import AlgoContext, OnboardingRequest, OnboardingScores, ScoreWithReason
from hmm_contracts.common import AreaScoreEventKind, QuizStatus, SubmissionStatus
from hmm_contracts.notifications import NotificationType
from hmm_core import models
from hmm_core.onboarding.baseline import load_onboarding_baseline
from hmm_core.onboarding.jobs import score_onboarding
from hmm_core.platform.jobs import JobName, defer_job
from hmm_core.scores.service import write_calculated_scores
from sqlalchemy import func, select, update

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


async def seed_quiz(h: Harness, admin: models.User, *, drop: str | None = None) -> models.Quiz:
    async with h.db() as session:
        await session.execute(
            update(models.Quiz)
            .where(models.Quiz.status == QuizStatus.PUBLISHED)
            .values(status=QuizStatus.ARCHIVED)
        )
        await session.flush()
        version = await session.scalar(select(func.coalesce(func.max(models.Quiz.version), 0)))
        quiz = models.Quiz(
            version=(version or 0) + 1,
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


async def submit(
    h: Harness, user: models.User, quiz: models.Quiz, answers: dict[str, Any]
) -> models.OnboardingSubmission:
    async with h.db() as session:
        row = models.OnboardingSubmission(
            user_id=user.id,
            quiz_id=quiz.id,
            answers={key: {"value": value, "other": None} for key, value in answers.items()},
            status=SubmissionStatus.SUBMITTED,
            submitted_at=h.now,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        return row


def record(h: Harness, monkeypatch: pytest.MonkeyPatch) -> list[OnboardingRequest]:
    captured: list[OnboardingRequest] = []
    original = h.algorithm.score_onboarding

    async def wrapper(ctx: AlgoContext, request: OnboardingRequest) -> OnboardingScores:
        captured.append(request)
        return await original(ctx, request)

    monkeypatch.setattr(h.algorithm, "score_onboarding", wrapper)
    return captured


async def settle(h: Harness, user: models.User) -> None:
    async with h.db() as session:
        session.add(
            models.GithubSnapshot(
                user_id=user.id, fetched_at=h.now, status="error", error="rate limited"
            )
        )


def cv_file(h: Harness, user: models.User, **fields: Any) -> models.CvFile:
    return models.CvFile(
        user_id=user.id,
        object_key=f"cv/{user.id}",
        filename="cv.md",
        mime="text/markdown",
        size_bytes=10,
        uploaded_at=h.now,
        text_error=None,
        **fields,
    )


async def _setup(h: Harness) -> tuple[models.User, models.Quiz, models.User]:
    await h.seed_areas(("backend", "frontend"))
    admin = await h.admin()
    quiz = await seed_quiz(h, admin)
    member = await h.user()
    return admin, quiz, member


async def _scores(h: Harness, user_id: UUID) -> dict[str, models.AreaScore]:
    async with h.db() as session:
        rows = await session.execute(
            select(models.Area.key, models.AreaScore)
            .join(models.Area, models.Area.id == models.AreaScore.area_id)
            .where(models.AreaScore.user_id == user_id)
        )
        return {key: score for key, score in rows}


async def _events(h: Harness, user_id: UUID) -> list[tuple[str, models.AreaScoreEvent]]:
    async with h.db() as session:
        rows = await session.execute(
            select(models.Area.key, models.AreaScoreEvent)
            .join(models.Area, models.Area.id == models.AreaScoreEvent.area_id)
            .where(models.AreaScoreEvent.user_id == user_id)
            .order_by(models.AreaScoreEvent.created_at, models.AreaScoreEvent.id)
        )
        return [(key, event) for key, event in rows]


async def _notifications(h: Harness, user_id: UUID) -> list[models.Notification]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.Notification).where(
                    models.Notification.user_id == user_id,
                    models.Notification.type == NotificationType.SCORE_CHANGED,
                )
            )
        )


async def _baseline(h: Harness, user_id: UUID) -> Any:
    async with h.db() as session:
        return await load_onboarding_baseline(session, user_id)


async def test_job_is_registered_and_waits(harness: Harness) -> None:
    h = harness
    _, quiz, member = await _setup(h)
    await submit(h, member, quiz, {"languages": ["python"]})
    async with h.db() as session:
        await defer_job(session, h.jobs, JobName.SCORE_ONBOARDING, user_id=str(member.id))
    await h.run_jobs()
    assert await _scores(h, member.id) == {}
    assert await _baseline(h, member.id) is None
    pending = [
        job for job in await h.pending_jobs("score_onboarding") if job["status"] != "succeeded"
    ]
    assert len(pending) == 1
    assert pending[0]["status"] == "todo"
    assert pending[0]["args"]["user_id"] == str(member.id)
    async with h.db() as session:
        user = await session.get(models.User, member.id)
        assert user is not None
        assert user.onboarding_status == "completed"
    assert await _notifications(h, member.id) == []


async def test_deadline_scores_without_github_or_cv(harness: Harness) -> None:
    h = harness
    _, quiz, member = await _setup(h)
    async with h.db() as session:
        row = models.OnboardingSubmission(
            user_id=member.id,
            quiz_id=quiz.id,
            answers={"languages": {"value": ["python"], "other": None}},
            status=SubmissionStatus.SUBMITTED,
            submitted_at=h.now - timedelta(minutes=11),
        )
        session.add(row)
    await score_onboarding(h.runtime, str(member.id))
    baseline = await _baseline(h, member.id)
    assert baseline is not None
    assert baseline.submitted_at == h.now - timedelta(minutes=11)
    assert baseline.area_scores["backend"].value == 10.0
    assert baseline.area_scores["frontend"].value == 0.0
    scores = await _scores(h, member.id)
    assert set(scores) == {"backend", "frontend"}
    assert all(score.calc_reason for score in scores.values())
    assert scores["backend"].calculated == Decimal("10.00")
    events = await _events(h, member.id)
    assert len(events) == 1
    key, event = events[0]
    assert key == "backend"
    assert event.kind == AreaScoreEventKind.CALCULATED
    assert event.old_effective == Decimal("0.00")
    assert event.new_effective == Decimal("10.00")
    assert event.algo_version == "fake-1"
    notifications = await _notifications(h, member.id)
    assert len(notifications) == 1
    payload = notifications[0].payload
    assert isinstance(payload, dict)
    assert isinstance(payload["changes"], list)
    assert len(payload["changes"]) == 1
    args = {"user_id": str(member.id)}
    assert [job["args"] for job in await h.pending_jobs("recompute_member")] == [args]
    assert [job["args"] for job in await h.pending_jobs("compute_lead_fits_for_member")] == [args]
    async with h.db() as session:
        review = await session.get(models.MemberReview, member.id)
        assert review is not None
        assert review.discrepancies == ["High quiz score in backend without CV or GitHub evidence"]


async def test_ready_states(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    h = harness
    _, quiz, member = await _setup(h)
    captured = record(h, monkeypatch)
    await submit(h, member, quiz, {"languages": ["python"]})
    async with h.db() as session:
        session.add(
            models.GithubSnapshot(
                user_id=member.id,
                fetched_at=h.now,
                status="ok",
                profile={"login": "u", "account_created_at": "2020-01-01T00:00:00Z"},
                repos=[],
                languages={"python": 5000},
                stats={
                    "followers": 1,
                    "contributions_last_year": 2,
                    "public_repos": 3,
                    "stars_total": 4,
                },
            )
        )
        session.add(cv_file(h, member, text_status="pending", text=None))
    await score_onboarding(h.runtime, str(member.id))
    assert captured == []
    assert await _scores(h, member.id) == {}
    assert [job["status"] for job in await h.pending_jobs("score_onboarding")] == ["todo"]
    async with h.db() as session:
        await session.execute(
            update(models.CvFile)
            .where(models.CvFile.user_id == member.id)
            .values(text_status="ok", text="backend")
        )
    await score_onboarding(h.runtime, str(member.id))
    assert len(captured) == 1
    assert captured[0].cv_text == "backend"
    assert captured[0].github is not None
    assert captured[0].github.login == "u"
    assert captured[0].github.followers == 1
    assert set(await _scores(h, member.id)) == {"backend", "frontend"}
    other = await h.user()
    await submit(h, other, quiz, {"languages": ["react"]})
    await settle(h, other)
    await score_onboarding(h.runtime, str(other.id))
    assert len(captured) == 2
    assert captured[1].github is None
    assert captured[1].cv_text is None
    assert set(await _scores(h, other.id)) == {"backend", "frontend"}


async def test_hidden_answers_never_reach_algo(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness
    await h.seed_areas(("backend", "frontend"))
    admin = await h.admin()
    await seed_quiz(h, admin)
    member = await h.user(onboarded=False)
    async with await h.client(member) as client:
        response = await client.put(
            "/api/v1/onboarding/answers",
            json={
                "answers": {
                    "languages": {"value": ["react"]},
                    "years": {"value": 3},
                    "direction": {"value": "backend"},
                }
            },
        )
        assert response.status_code == 200, response.text
        response = await client.post("/api/v1/onboarding/submit")
        assert response.status_code == 200, response.text
    await settle(h, member)
    captured = record(h, monkeypatch)
    await score_onboarding(h.runtime, str(member.id))
    assert len(captured) == 1
    assert set(captured[0].answers.answers) == {"languages", "years"}
    scores = await _scores(h, member.id)
    assert scores["backend"].calculated == Decimal("0.00")
    assert scores["frontend"].calculated == Decimal("8.00")


async def test_retry_has_no_duplicate_effects(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness
    _, quiz, member = await _setup(h)
    await submit(h, member, quiz, {"languages": ["python"], "direction": "backend"})
    await settle(h, member)

    async def broken(ctx: AlgoContext, request: OnboardingRequest) -> OnboardingScores:
        raise RuntimeError("algo down")

    with monkeypatch.context() as patch:
        patch.setattr(h.algorithm, "score_onboarding", broken)
        with pytest.raises(RuntimeError):
            await score_onboarding(h.runtime, str(member.id))
    assert await _baseline(h, member.id) is None
    assert await _scores(h, member.id) == {}
    assert await _events(h, member.id) == []
    await score_onboarding(h.runtime, str(member.id))
    baseline = await _baseline(h, member.id)
    scores = await _scores(h, member.id)
    events = await _events(h, member.id)
    notifications = await _notifications(h, member.id)
    assert set(scores) == {"backend", "frontend"}
    assert [key for key, _ in events] == ["backend"]
    await score_onboarding(h.runtime, str(member.id))
    assert await _baseline(h, member.id) == baseline
    again = await _scores(h, member.id)
    assert {key: row.updated_at for key, row in again.items()} == {
        key: row.updated_at for key, row in scores.items()
    }
    assert [event.id for _, event in await _events(h, member.id)] == [
        event.id for _, event in events
    ]
    assert len(await _notifications(h, member.id)) == len(notifications)
    async with h.db() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(models.OnboardingBaseline)
            .where(models.OnboardingBaseline.user_id == member.id)
        )
    assert count == 1


async def test_baseline_is_immutable(harness: Harness) -> None:
    h = harness
    _, quiz, member = await _setup(h)
    await submit(h, member, quiz, {"languages": ["python"]})
    await settle(h, member)
    await score_onboarding(h.runtime, str(member.id))
    baseline = await _baseline(h, member.id)
    assert baseline is not None
    async with h.db() as session:
        areas = list(await session.scalars(select(models.Area)))
        await write_calculated_scores(
            session,
            member.id,
            {"backend": ScoreWithReason(value=9.0, reason="later facts")},
            areas=areas,
            algo_version="fake-1",
            now=h.now,
        )
    assert (await _scores(h, member.id))["backend"].calculated == Decimal("9.00")
    after = await _baseline(h, member.id)
    assert after is not None
    assert after.area_scores == baseline.area_scores
    assert after.submitted_at == baseline.submitted_at
    events = await _events(h, member.id)
    await score_onboarding(h.runtime, str(member.id))
    assert await _baseline(h, member.id) == baseline
    assert (await _scores(h, member.id))["backend"].calculated == Decimal("9.00")
    assert len(await _events(h, member.id)) == len(events)


async def test_step_two_acceptance_against_fake(harness: Harness) -> None:
    h = harness
    admin, quiz, first = await _setup(h)
    second = await h.user()
    await submit(h, first, quiz, {"languages": ["python"], "direction": "backend"})
    await submit(h, second, quiz, {"languages": ["react"]})
    for member in (first, second):
        await settle(h, member)
        await score_onboarding(h.runtime, str(member.id))
    one = await _scores(h, first.id)
    two = await _scores(h, second.id)
    assert one["backend"].calculated > two["backend"].calculated
    assert two["frontend"].calculated > one["frontend"].calculated
    async with await h.client(admin) as client:
        response = await client.put(
            f"/api/v1/admin/users/{first.id}/scores/backend/override",
            json={"value": 6.5, "reason": "Interview result", "evidence": "interview"},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["effective"] == 6.5
    assert body["override"] == 6.5
    assert body["calculated"] == 10.0
    async with await h.client(first) as client:
        response = await client.get("/api/v1/onboarding")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"
