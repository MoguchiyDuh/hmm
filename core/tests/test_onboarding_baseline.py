from datetime import timedelta
from decimal import Decimal

from harness import Harness
from hmm_contracts.algo import OnboardingBaseline as OnboardingBaselineData
from hmm_contracts.algo import ScoreWithReason
from hmm_contracts.common import AreaScoreEventKind
from hmm_core.models import AreaScoreEvent, OnboardingBaseline
from hmm_core.onboarding.baseline import load_onboarding_baseline, save_onboarding_baseline
from sqlalchemy import func, select


async def test_baseline_roundtrip(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    user = await h.user()
    baseline = OnboardingBaselineData(
        submitted_at=h.now,
        area_scores={
            "backend": ScoreWithReason(value=6.5, reason="quiz"),
            "data": ScoreWithReason(value=3.0, reason="cv"),
        },
        discrepancies=["hint"],
    )
    async with h.db() as session:
        await save_onboarding_baseline(session, user.id, baseline)
    async with h.db() as session:
        loaded = await load_onboarding_baseline(session, user.id)
        assert loaded == baseline
        assert loaded is not None
        assert loaded.submitted_at.utcoffset() == timedelta(0)


async def test_baseline_upsert_replaces_row(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    user = await h.user()
    baseline = OnboardingBaselineData(
        submitted_at=h.now,
        area_scores={
            "backend": ScoreWithReason(value=6.5, reason="quiz"),
            "data": ScoreWithReason(value=3.0, reason="cv"),
        },
        discrepancies=["hint"],
    )
    async with h.db() as session:
        await save_onboarding_baseline(session, user.id, baseline)
    async with h.db() as session:
        row = await session.get(OnboardingBaseline, user.id)
        assert row is not None
        created_at = row.created_at
        updated_at = row.updated_at
    replacement = OnboardingBaselineData(
        submitted_at=h.advance(timedelta(hours=1)),
        area_scores={"backend": ScoreWithReason(value=8.0, reason="updated quiz")},
        discrepancies=["replacement hint"],
    )
    async with h.db() as session:
        await save_onboarding_baseline(session, user.id, replacement)
    async with h.db() as session:
        assert await load_onboarding_baseline(session, user.id) == replacement
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OnboardingBaseline)
                .where(OnboardingBaseline.user_id == user.id)
            )
            == 1
        )
        row = await session.get(OnboardingBaseline, user.id)
        assert row is not None
        assert row.created_at == created_at
        assert row.updated_at > updated_at


async def test_baseline_missing(harness: Harness) -> None:
    h = harness
    user = await h.user()
    async with h.db() as session:
        assert await load_onboarding_baseline(session, user.id) is None


async def test_score_event_accepts_null_algo_version(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    user = await h.user()
    async with h.db() as session:
        event = AreaScoreEvent(
            user_id=user.id,
            area_id=areas["backend"].id,
            kind=AreaScoreEventKind.OVERRIDE_SET,
            old_effective=Decimal("0.00"),
            new_effective=Decimal("1.00"),
            reason="manual",
            algo_version=None,
            actor_id=None,
        )
        session.add(event)
        await session.flush()
        event_id = event.id
    async with h.db() as session:
        loaded = await session.get(AreaScoreEvent, event_id)
        assert loaded is not None
        assert loaded.algo_version is None


async def test_baseline_empty_scores_and_discrepancies(harness: Harness) -> None:
    h = harness
    user = await h.user()
    baseline = OnboardingBaselineData(submitted_at=h.now, area_scores={}, discrepancies=[])
    async with h.db() as session:
        await save_onboarding_baseline(session, user.id, baseline)
    async with h.db() as session:
        assert await load_onboarding_baseline(session, user.id) == baseline
