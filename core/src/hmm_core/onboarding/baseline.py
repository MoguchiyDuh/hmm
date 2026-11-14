from uuid import UUID

from hmm_contracts.algo import OnboardingBaseline as OnboardingBaselineData
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models


async def load_onboarding_baseline(
    session: AsyncSession, user_id: UUID
) -> OnboardingBaselineData | None:
    row = await session.get(models.OnboardingBaseline, user_id)
    if row is None:
        return None
    return OnboardingBaselineData.model_validate(
        {
            "submitted_at": row.submitted_at,
            "area_scores": row.area_scores,
            "discrepancies": row.discrepancies,
        }
    )


async def save_onboarding_baseline(
    session: AsyncSession, user_id: UUID, baseline: OnboardingBaselineData
) -> None:
    statement = insert(models.OnboardingBaseline).values(
        user_id=user_id,
        submitted_at=baseline.submitted_at,
        area_scores={
            key: score.model_dump(mode="json") for key, score in baseline.area_scores.items()
        },
        discrepancies=list(baseline.discrepancies),
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[models.OnboardingBaseline.user_id],
            set_={
                "submitted_at": statement.excluded.submitted_at,
                "area_scores": statement.excluded.area_scores,
                "discrepancies": statement.excluded.discrepancies,
                "updated_at": text("now()"),
            },
        )
    )
