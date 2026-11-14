import itertools
import re
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from harness import NOW, Harness
from hmm_algo.testing import FakeAlgorithm
from hmm_contracts.algo import AlgoContext, Rec, Recommendations, RecommendRequest
from hmm_contracts.common import (
    AreaImportance,
    EnrollmentSource,
    EventKind,
    EventOrigin,
    EventStatus,
    ProjectOrigin,
    ProjectStatus,
    ProjectVisibility,
)
from hmm_core import models
from hmm_core.catalog import service as catalog
from hmm_core.platform.jobs import JOB_MODULES, REGISTRY, JobName, defer_job
from hmm_core.recommendations import service
from hmm_core.recommendations.jobs import refresh_recommendations
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

_titles = itertools.count(1)


class Counting(FakeAlgorithm):
    version = "fake-1"

    def __init__(self, inner: FakeAlgorithm) -> None:
        self.inner = inner
        self.calls = 0

    async def recommend(self, ctx: AlgoContext, request: RecommendRequest) -> Recommendations:
        self.calls += 1
        return await self.inner.recommend(ctx, request)


class Failing(FakeAlgorithm):
    version = "bad-1"

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def recommend(self, ctx: AlgoContext, request: RecommendRequest) -> Recommendations:
        raise self.error


async def make_hackathon(
    h: Harness,
    *,
    starts_in: timedelta = timedelta(days=30),
    areas: tuple[tuple[str, AreaImportance], ...] = (("backend", AreaImportance.PRIMARY),),
) -> models.Event:
    async with h.db() as session:
        return await catalog.create_event(
            session,
            catalog.EventInput(
                kind=EventKind.HACKATHON,
                title=f"Rec Job Hack {next(_titles)}",
                starts_at=h.now + starts_in,
                ends_at=h.now + starts_in + timedelta(days=2),
                timezone="UTC",
                registration_deadline=None,
                areas=[
                    catalog.EventAreaInput(key=key, importance=importance)
                    for key, importance in areas
                ],
            ),
            origin=EventOrigin.MANUAL,
            status=EventStatus.PUBLISHED,
            created_by=None,
            now=h.now,
        )


async def team(
    h: Harness,
    event: models.Event,
    lead: models.User,
    *members: models.User,
    team_status: str = "forming",
) -> models.Project:
    async with h.db() as session:
        project = models.Project(
            event_id=event.id,
            name=f"Team {next(_titles)}",
            description_md="",
            goal="",
            links=[],
            visibility=ProjectVisibility.PUBLIC.value,
            recruiting=None,
            lead_user_id=lead.id,
            status=ProjectStatus.ACTIVE.value
            if team_status in {"locked", "finished"}
            else ProjectStatus.IDEA.value,
            team_status=team_status,
            origin=ProjectOrigin.CLAIM.value,
        )
        session.add(project)
        await session.flush()
        for user in (lead, *members):
            session.add(
                models.ProjectMember(
                    project_id=project.id,
                    event_id=event.id,
                    user_id=user.id,
                    role="lead" if user is lead else "member",
                    state="confirmed",
                    trust="self",
                    joined_via="claim",
                    is_team_row=True,
                    joined_at=h.now,
                )
            )
        await session.flush()
        await session.refresh(project)
        return project


async def enroll(
    session: AsyncSession,
    event: models.Event,
    user: models.User,
    status: str = "active",
    **fields: Any,
) -> models.Enrollment:
    enrollment = models.Enrollment(
        event_id=event.id,
        user_id=user.id,
        status=status,
        source=EnrollmentSource.SELF.value,
        status_at=NOW,
        **fields,
    )
    session.add(enrollment)
    await session.flush()
    await session.refresh(enrollment)
    return enrollment


async def standalone(
    h: Harness,
    lead: models.User,
    *,
    recruiting: dict[str, Any] | None = None,
    default_recruiting: bool = True,
) -> models.Project:
    async with h.db() as session:
        project = models.Project(
            event_id=None,
            name=f"Solo {next(_titles)}",
            description_md="",
            goal="",
            links=[],
            visibility=ProjectVisibility.PUBLIC.value,
            recruiting=recruiting
            if recruiting is not None or not default_recruiting
            else {"areas": ["backend"], "note": "hiring"},
            lead_user_id=lead.id,
            status=ProjectStatus.IDEA.value,
            team_status=None,
            origin=ProjectOrigin.CLAIM.value,
        )
        session.add(project)
        await session.flush()
        session.add(
            models.ProjectMember(
                project_id=project.id,
                user_id=lead.id,
                role="lead",
                state="confirmed",
                trust="self",
                joined_via="create",
                is_team_row=False,
                joined_at=h.now,
            )
        )
        await session.flush()
        await session.refresh(project)
        return project


async def score(h: Harness, area: models.Area, user: models.User, value: str) -> None:
    async with h.db() as session:
        session.add(
            models.AreaScore(
                user_id=user.id, area_id=area.id, calculated=Decimal(value), calc_reason="x"
            )
        )


async def rows_of(h: Harness, project: models.Project) -> list[models.RecommendationSet]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.RecommendationSet)
                .where(models.RecommendationSet.subject_id == project.id)
                .order_by(models.RecommendationSet.created_at, models.RecommendationSet.id)
            )
        )


async def open_alerts(h: Harness) -> list[models.AdminAlert]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.AdminAlert).where(
                    models.AdminAlert.type == "algo_failure", models.AdminAlert.status == "open"
                )
            )
        )


def ids_of(items: Any) -> list[str]:
    return [item["user_id"] for item in items]


async def forming_setup(
    h: Harness,
) -> tuple[models.Project, models.Event, dict[str, models.Area], list[models.User]]:
    areas = await h.seed_areas()
    event = await make_hackathon(h)
    lead = await h.user()
    member = await h.user()
    low = await h.user()
    high = await h.user()
    plain_low = await h.user()
    plain_high = await h.user()
    project = await team(h, event, lead, member)
    async with h.db() as session:
        session.add(models.ProjectArea(project_id=project.id, area_id=areas["backend"].id))
        await enroll(session, event, low)
        await enroll(session, event, high)
    for user, value in ((low, "2"), (high, "8"), (plain_low, "3"), (plain_high, "9")):
        await score(h, areas["backend"], user, value)
    return project, event, areas, [low, high, plain_low, plain_high]


def test_registration() -> None:
    assert JobName.REFRESH_RECOMMENDATIONS in REGISTRY
    assert "hmm_core.recommendations.jobs" in JOB_MODULES


async def test_happy_path_team_forming(harness: Harness) -> None:
    h = harness
    project, _, _, (low, high, plain_low, plain_high) = await forming_setup(h)
    await refresh_recommendations(h.runtime, project_id=str(project.id))
    rows = await rows_of(h, project)
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "ready"
    assert row.subject_type == "project"
    assert re.fullmatch(r"[0-9a-f]{64}", row.input_hash)
    assert row.algo_version == "fake-1"
    assert row.completed_at == h.now
    assert row.error is None
    assert ids_of(row.enrolled) == [str(high.id), str(low.id)]
    assert ids_of(row.others) == [str(plain_high.id), str(plain_low.id)]
    assert isinstance(row.enrolled, list) and isinstance(row.others, list)
    for item in [*row.enrolled, *row.others]:
        rec = Rec.model_validate(item)
        assert 0 <= rec.score <= 1
        assert rec.reasons
        assert rec.tags == []


async def test_matching_input_is_a_noop(harness: Harness) -> None:
    h = harness
    project, _, _, _ = await forming_setup(h)
    algorithm = Counting(FakeAlgorithm())
    runtime = replace(h.runtime, algorithm=algorithm)
    await refresh_recommendations(runtime, project_id=str(project.id))
    assert algorithm.calls == 1
    [first] = await rows_of(h, project)
    await refresh_recommendations(runtime, project_id=str(project.id))
    [again] = await rows_of(h, project)
    assert algorithm.calls == 1
    assert again.id == first.id
    assert again.input_hash == first.input_hash
    assert again.created_at == first.created_at


async def test_input_changes_recompute(harness: Harness) -> None:
    h = harness
    project, event, areas, (_, _, plain_low, _) = await forming_setup(h)
    algorithm = Counting(FakeAlgorithm())
    runtime = replace(h.runtime, algorithm=algorithm)
    await refresh_recommendations(runtime, project_id=str(project.id))
    third = await h.user()
    async with h.db() as session:
        await enroll(session, event, third)
    await refresh_recommendations(runtime, project_id=str(project.id))
    rows = await rows_of(h, project)
    assert algorithm.calls == 2
    assert [row.status for row in rows] == ["ready", "ready"]
    assert rows[0].input_hash != rows[1].input_hash
    assert str(third.id) in ids_of(rows[1].enrolled)
    async with h.db() as session:
        await session.execute(
            update(models.AreaScore)
            .where(
                models.AreaScore.user_id == plain_low.id,
                models.AreaScore.area_id == areas["backend"].id,
            )
            .values(
                override=Decimal("10"),
                override_evidence="other",
                override_reason="x",
                override_at=h.now,
            )
        )
    await refresh_recommendations(runtime, project_id=str(project.id))
    rows = await rows_of(h, project)
    assert algorithm.calls == 3
    assert len(rows) == 3
    assert len({row.input_hash for row in rows}) == 3
    assert ids_of(rows[2].others)[0] == str(plain_low.id)


async def test_pending_row_committed_before_algo_call(harness: Harness) -> None:
    h = harness
    project, _, _, _ = await forming_setup(h)
    seen: list[int] = []

    class Peeking(FakeAlgorithm):
        async def recommend(self, ctx: AlgoContext, request: RecommendRequest) -> Recommendations:
            async with h.sessionmaker() as session:
                seen.append(
                    await session.scalar(
                        select(func.count())
                        .select_from(models.RecommendationSet)
                        .where(
                            models.RecommendationSet.subject_id == project.id,
                            models.RecommendationSet.status == "pending",
                        )
                    )
                    or 0
                )
            return await super().recommend(ctx, request)

    await refresh_recommendations(
        replace(h.runtime, algorithm=Peeking()), project_id=str(project.id)
    )
    assert seen == [1]
    assert [row.status for row in await rows_of(h, project)] == ["ready"]


def test_validated_recommendations() -> None:
    a = uuid4()
    b = uuid4()
    result = Recommendations.model_construct(
        enrolled=[
            Rec.model_construct(
                user_id=a, score=2.5, reasons=[" ok "], tags=["newcomer"], suggested_area="backend"
            ),
            Rec.model_construct(
                user_id=a, score=0.1, reasons=["dup"], tags=[], suggested_area=None
            ),
            Rec.model_construct(
                user_id=uuid4(), score=0.5, reasons=["x"], tags=[], suggested_area=None
            ),
            Rec.model_construct(
                user_id=a, score=0.4, reasons=["   "], tags=[], suggested_area=None
            ),
        ],
        others=[
            Rec.model_construct(
                user_id=b, score=-1.0, reasons=["negative"], tags=[], suggested_area=None
            )
        ],
    )
    validated, dropped = service.validated_recommendations(result, frozenset({a}), frozenset({b}))
    assert dropped == 3
    assert validated.enrolled == [
        Rec(user_id=a, score=1.0, reasons=["ok"], tags=["newcomer"], suggested_area="backend")
    ]
    assert validated.others == [
        Rec(user_id=b, score=0.0, reasons=["negative"], tags=[], suggested_area=None)
    ]


async def test_invalid_output_is_filtered(harness: Harness) -> None:
    h = harness
    project, _, _, (low, high, plain_low, _) = await forming_setup(h)

    class Sloppy(FakeAlgorithm):
        async def recommend(self, ctx: AlgoContext, request: RecommendRequest) -> Recommendations:
            def item(user_id: UUID, reasons: list[str]) -> Rec:
                return Rec.model_construct(
                    user_id=user_id, score=0.5, reasons=reasons, tags=[], suggested_area=None
                )

            return Recommendations.model_construct(
                enrolled=[
                    item(high.id, ["good"]),
                    item(high.id, ["again"]),
                    item(uuid4(), ["ghost"]),
                    item(low.id, [" "]),
                    item(plain_low.id, ["wrong group"]),
                ],
                others=[item(plain_low.id, ["fine"]), item(high.id, ["wrong group"])],
            )

    await refresh_recommendations(
        replace(h.runtime, algorithm=Sloppy()), project_id=str(project.id)
    )
    [row] = await rows_of(h, project)
    assert row.status == "ready"
    assert ids_of(row.enrolled) == [str(high.id)]
    assert ids_of(row.others) == [str(plain_low.id)]
    assert await open_alerts(h) == []


@pytest.mark.parametrize(
    "error", [RuntimeError("boom"), TimeoutError("recommend timed out")], ids=["boom", "timeout"]
)
async def test_failure_marks_row_and_alerts(harness: Harness, error: Exception) -> None:
    h = harness
    project, _, _, _ = await forming_setup(h)
    runtime = replace(h.runtime, algorithm=Failing(error))
    with pytest.raises(type(error)):
        await refresh_recommendations(runtime, project_id=str(project.id))
    [row] = await rows_of(h, project)
    assert row.status == "failed"
    assert row.error is not None and str(error) in row.error
    assert row.completed_at == h.now
    assert row.algo_version == "bad-1"
    assert row.enrolled is None and row.others is None
    [alert] = await open_alerts(h)
    assert alert.severity == "critical"
    assert alert.subject_type == "project"
    assert alert.subject_id == project.id
    assert isinstance(alert.payload, dict)
    for key in ("title", "message", "link"):
        assert alert.payload[key]
    assert alert.dedupe_key == f"algo_failure:recommend:{project.id}:{h.now:%Y-%m-%d-%H}"
    with pytest.raises(type(error)):
        await refresh_recommendations(runtime, project_id=str(project.id))
    assert [row.status for row in await rows_of(h, project)] == ["failed", "failed"]
    assert len(await open_alerts(h)) == 1


async def test_retry_after_failure_succeeds(harness: Harness) -> None:
    h = harness
    project, _, _, _ = await forming_setup(h)
    with pytest.raises(RuntimeError):
        await refresh_recommendations(
            replace(h.runtime, algorithm=Failing(RuntimeError("boom"))),
            project_id=str(project.id),
        )
    algorithm = Counting(FakeAlgorithm())
    await refresh_recommendations(
        replace(h.runtime, algorithm=algorithm), project_id=str(project.id)
    )
    assert algorithm.calls == 1
    assert [row.status for row in await rows_of(h, project)] == ["failed", "ready"]
    assert len(await open_alerts(h)) == 1


async def test_non_refreshable_and_unknown(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    finished = await team(h, event, await h.user(), team_status="finished")
    closed = await standalone(h, await h.user(), default_recruiting=False)
    for project_id in (finished.id, closed.id, uuid4()):
        await refresh_recommendations(h.runtime, project_id=str(project_id))
    async with h.db() as session:
        assert await session.scalar(select(func.count()).select_from(models.RecommendationSet)) == 0


async def test_refresh_is_debounced(harness: Harness) -> None:
    h = harness
    project, _, _, _ = await forming_setup(h)
    async with h.db() as session:
        first = await defer_job(
            session, h.jobs, JobName.REFRESH_RECOMMENDATIONS, project_id=str(project.id)
        )
        second = await defer_job(
            session, h.jobs, JobName.REFRESH_RECOMMENDATIONS, project_id=str(project.id)
        )
    assert first is not None
    assert second is None
    pending = await h.pending_jobs("refresh_recommendations")
    assert len(pending) == 1
    assert pending[0]["args"] == {"project_id": str(project.id)}
    await h.run_jobs()
    assert [row.status for row in await rows_of(h, project)] == ["ready"]
    async with h.db() as session:
        await defer_job(
            session, h.jobs, JobName.REFRESH_RECOMMENDATIONS, project_id=str(project.id)
        )
    await h.run_jobs()
    assert len(await rows_of(h, project)) == 1
