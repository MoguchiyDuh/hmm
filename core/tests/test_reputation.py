from uuid import UUID, uuid4

from harness import Harness
from hmm_contracts.algo import RatingSummary
from hmm_contracts.common import ReviewContext, ReviewSubjectType
from hmm_core.models import Review
from hmm_core.scores.reputation import empty_reputation, reputation_summaries, reputation_summary


async def add_reviews(
    harness: Harness,
    reviewee: UUID,
    reviews: list[tuple[ReviewContext, int | None, bool, bool]],
) -> None:
    async with harness.db() as session:
        for context, rating, revealed, removed in reviews:
            reviewer = await harness.user()
            session.add(
                Review(
                    subject_type=ReviewSubjectType.PROJECT,
                    subject_id=uuid4(),
                    reviewer_id=reviewer.id,
                    reviewee_id=reviewee,
                    context=context,
                    rating=rating,
                    comment="",
                    revealed_at=harness.now if revealed else None,
                    removed_at=harness.now if removed else None,
                )
            )


async def test_reputation_counts_only_visible_ratings(harness: Harness) -> None:
    member = await harness.user()
    await add_reviews(
        harness,
        member.id,
        [
            (ReviewContext.MEMBER, 8, True, False),
            (ReviewContext.MEMBER, 7, True, False),
            (ReviewContext.MEMBER, 2, True, True),
            (ReviewContext.MEMBER, 1, False, False),
            (ReviewContext.MENTOR, None, True, False),
            (ReviewContext.LEAD, 10, True, False),
        ],
    )
    async with harness.db() as session:
        reputation = await reputation_summary(session, member.id)
    assert list(reputation) == [
        ReviewContext.MEMBER,
        ReviewContext.LEAD,
        ReviewContext.MENTOR,
        ReviewContext.HOST,
    ]
    assert reputation[ReviewContext.MEMBER] == RatingSummary(avg=7.5, count=2)
    assert reputation[ReviewContext.LEAD] == RatingSummary(avg=10.0, count=1)
    assert reputation[ReviewContext.MENTOR] == RatingSummary(avg=None, count=0)
    assert reputation[ReviewContext.HOST] == RatingSummary(avg=None, count=0)


async def test_host_merges_speaker_and_needs_three_ratings(harness: Harness) -> None:
    host = await harness.user()
    await add_reviews(
        harness,
        host.id,
        [(ReviewContext.HOST, 9, True, False), (ReviewContext.SPEAKER, 6, True, False)],
    )
    async with harness.db() as session:
        assert (await reputation_summary(session, host.id))[ReviewContext.HOST] == RatingSummary(
            avg=None, count=2
        )
    await add_reviews(harness, host.id, [(ReviewContext.SPEAKER, 7, True, False)])
    async with harness.db() as session:
        assert (await reputation_summary(session, host.id))[ReviewContext.HOST] == RatingSummary(
            avg=7.33, count=3
        )


async def test_batch_returns_every_requested_user(harness: Harness) -> None:
    first = await harness.user()
    second = await harness.user()
    await add_reviews(harness, first.id, [(ReviewContext.MEMBER, 5, True, False)])
    async with harness.db() as session:
        result = await reputation_summaries(session, [first.id, second.id, first.id])
        assert await reputation_summaries(session, []) == {}
    assert list(result) == [first.id, second.id]
    assert result[first.id][ReviewContext.MEMBER] == RatingSummary(avg=5.0, count=1)
    assert result[second.id] == empty_reputation()
