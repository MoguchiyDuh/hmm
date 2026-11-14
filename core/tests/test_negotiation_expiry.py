from datetime import timedelta
from typing import Any, cast
from uuid import UUID

import pytest
from harness import Harness
from hmm_contracts.common import MergeProposalStatus, SwapRequestStatus, TeamStatus
from hmm_core import models
from hmm_core.formation import service as formation
from sqlalchemy import func, select, update
from test_placement_confirmation import add_member, enroll, seed_project
from test_swaps import two_teams
from test_team_merges import merge_teams


async def swap_between(
    h: Harness,
    event: models.Event,
    team_a: models.Project,
    team_b: models.Project,
    lead_a: models.User,
    a1: models.User,
    b1: models.User,
) -> models.SwapRequest:
    async with await h.client(lead_a) as client:
        response = await client.post(
            f"/api/v1/projects/{team_a.id}/swaps",
            json={
                "from_user_id": str(a1.id),
                "to_user_id": str(b1.id),
            },
        )
    assert response.status_code == 201, response.text
    async with h.db() as session:
        row = await session.get_one(models.SwapRequest, UUID(response.json()["id"]))
        assert row.event_id == event.id
        assert row.to_team_id == team_b.id
        return row


async def sweep(h: Harness, event: models.Event, *, limit: int = 200) -> int:
    async with h.db() as session:
        locked_event = (
            await session.scalars(
                select(models.Event).where(models.Event.id == event.id).with_for_update()
            )
        ).one()
        return await formation.expire_due_swaps_and_merges(
            session, locked_event, h.now, limit=limit
        )


async def results(h: Harness, kind: str) -> list[models.Notification]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.Notification).where(models.Notification.type == kind)
            )
        )


def payload(row: models.Notification) -> dict[str, Any]:
    return cast(dict[str, Any], row.payload)


async def test_swap_expiry_and_repeat_safety(harness: Harness) -> None:
    h = harness
    event, team_a, team_b, lead_a, lead_b, a1, _, b1 = await two_teams(
        h, team_a_ends=h.now + timedelta(hours=1), team_b_ends=h.now + timedelta(hours=5)
    )
    swap = await swap_between(h, event, team_a, team_b, lead_a, a1, b1)
    h.advance(timedelta(minutes=30))
    assert await sweep(h, event) == 0
    async with h.db() as session:
        assert (
            await session.get_one(models.SwapRequest, swap.id)
        ).status == SwapRequestStatus.PENDING.value
    assert await results(h, "swap_result") == []
    h.advance(timedelta(minutes=30))
    assert await sweep(h, event) == 1
    async with h.db() as session:
        row = await session.get_one(models.SwapRequest, swap.id)
        assert row.status == SwapRequestStatus.EXPIRED.value
        updated_at = row.updated_at
    rows = await results(h, "swap_result")
    assert len(rows) == 4
    assert {row.user_id for row in rows} == {lead_a.id, lead_b.id, a1.id, b1.id}
    assert all(
        payload(row)["status"] == "expired" and payload(row)["reason"] == "The review window ended"
        for row in rows
    )
    for _ in range(3):
        assert await sweep(h, event) == 0
    assert {row.id for row in await results(h, "swap_result")} == {row.id for row in rows}
    async with h.db() as session:
        assert (await session.get_one(models.SwapRequest, swap.id)).updated_at == updated_at
        assert await session.scalar(select(func.count()).select_from(models.AuditLog)) == 0
        assert await session.scalar(select(func.count()).select_from(models.ProjectMember)) == 5
        assert (
            await session.scalar(
                select(func.count())
                .select_from(models.ProjectMember)
                .where(models.ProjectMember.left_at.is_not(None))
            )
            == 0
        )
    assert not [job for job in await h.pending_jobs() if job["status"] == "todo"]


async def test_merge_expiry(harness: Harness) -> None:
    h = harness
    event, absorbed, target, lead_a, lead_b, _ = await merge_teams(h)
    async with h.db() as session:
        for team, hours in ((absorbed, 1), (target, 3)):
            await session.execute(
                update(models.Project)
                .where(models.Project.id == team.id)
                .values(review_ends_at=h.now + timedelta(hours=hours))
            )
    async with await h.client(lead_a) as client:
        response = await client.post(
            f"/api/v1/projects/{absorbed.id}/merges", json={"target_team_id": str(target.id)}
        )
    assert response.status_code == 201, response.text
    h.advance(timedelta(hours=1))
    assert await sweep(h, event) == 1
    async with h.db() as session:
        assert (
            await session.get_one(models.MergeProposal, UUID(response.json()["id"]))
        ).status == MergeProposalStatus.EXPIRED.value
    rows = await results(h, "merge_result")
    assert len(rows) == 2
    assert {row.user_id for row in rows} == {lead_a.id, lead_b.id}
    assert all(
        payload(row)["status"] == "expired" and payload(row)["reason"] == "The review window ended"
        for row in rows
    )
    assert await sweep(h, event) == 0
    assert len(await results(h, "merge_result")) == 2
    assert not [job for job in await h.pending_jobs() if job["status"] == "todo"]


async def test_nonreview_teams_close_negotiations(harness: Harness) -> None:
    h = harness
    event, team_a, team_b, lead_a, _, a1, _, b1 = await two_teams(h)
    swap = await swap_between(h, event, team_a, team_b, lead_a, a1, b1)
    async with h.db() as session:
        merge = models.MergeProposal(
            event_id=event.id,
            absorbed_team_id=team_a.id,
            target_team_id=team_b.id,
            proposed_by="system",
            status="pending",
        )
        session.add(merge)
        await session.execute(
            update(models.Project)
            .where(models.Project.id == team_b.id)
            .values(team_status=TeamStatus.LOCKED.value)
        )
        await session.flush()
        merge_id = merge.id
    assert await sweep(h, event) == 2
    async with h.db() as session:
        assert (await session.get_one(models.SwapRequest, swap.id)).status == "closed"
        assert (await session.get_one(models.MergeProposal, merge_id)).status == "closed"
    swap_results = await results(h, "swap_result")
    merge_results = await results(h, "merge_result")
    assert len(swap_results) == 4
    assert len(merge_results) == 2
    assert all(
        payload(row)["status"] == "closed"
        and payload(row)["reason"] == "The teams are no longer in review"
        for row in swap_results + merge_results
    )
    assert await sweep(h, event) == 0


@pytest.mark.parametrize("event_fallback", [False, True])
async def test_negotiation_deadline_fallbacks(harness: Harness, event_fallback: bool) -> None:
    h = harness
    event, team_a, team_b, lead_a, _, a1, _, b1 = await two_teams(h)
    swap = await swap_between(h, event, team_a, team_b, lead_a, a1, b1)
    deadline = event.starts_at if event_fallback else h.now + timedelta(hours=2)
    async with h.db() as session:
        await session.execute(
            update(models.Project)
            .where(models.Project.id.in_([team_a.id, team_b.id]))
            .values(review_ends_at=None)
        )
        await session.execute(
            update(models.EventFormation)
            .where(models.EventFormation.event_id == event.id)
            .values(review_ends_at=None if event_fallback else deadline)
        )
    h.advance(deadline - h.now - timedelta(seconds=1))
    assert await sweep(h, event) == 0
    h.advance(timedelta(seconds=1))
    assert await sweep(h, event) == 1
    async with h.db() as session:
        assert (await session.get_one(models.SwapRequest, swap.id)).status == "expired"


@pytest.mark.parametrize("status", ["accepted", "declined", "expired", "closed"])
async def test_terminal_negotiations_untouched(harness: Harness, status: str) -> None:
    h = harness
    event, team_a, team_b, lead_a, _, a1, _, b1 = await two_teams(h)
    async with h.db() as session:
        swap = models.SwapRequest(
            event_id=event.id,
            from_team_id=team_a.id,
            to_team_id=team_b.id,
            from_user_id=a1.id,
            to_user_id=b1.id,
            requested_by=lead_a.id,
            status=status,
        )
        merge = models.MergeProposal(
            event_id=event.id,
            absorbed_team_id=team_a.id,
            target_team_id=team_b.id,
            proposed_by="system",
            status=status,
        )
        session.add_all([swap, merge])
        await session.flush()
        await session.refresh(swap)
        await session.refresh(merge)
        swap_id, merge_id, swap_updated, merge_updated = (
            swap.id,
            merge.id,
            swap.updated_at,
            merge.updated_at,
        )
    h.advance(timedelta(days=2))
    assert await sweep(h, event) == 0
    async with h.db() as session:
        stored_swap = await session.get_one(models.SwapRequest, swap_id)
        stored_merge = await session.get_one(models.MergeProposal, merge_id)
        assert (stored_swap.status, stored_swap.updated_at) == (status, swap_updated)
        assert (stored_merge.status, stored_merge.updated_at) == (status, merge_updated)
    assert await results(h, "swap_result") == []
    assert await results(h, "merge_result") == []


async def test_lock_team_closes_negotiations(harness: Harness) -> None:
    h = harness
    event, team_a, team_b, lead_a, lead_b, a1, _, b1 = await two_teams(h)
    swap = await swap_between(h, event, team_a, team_b, lead_a, a1, b1)
    async with h.db() as session:
        merge = models.MergeProposal(
            event_id=event.id,
            absorbed_team_id=team_a.id,
            target_team_id=team_b.id,
            proposed_by="system",
            status="pending",
        )
        session.add(merge)
        await session.flush()
        merge_id = merge.id
        locked_event = (
            await session.scalars(
                select(models.Event).where(models.Event.id == event.id).with_for_update()
            )
        ).one()
        project = await session.get_one(models.Project, team_a.id)
        assert await formation.lock_team(
            session, h.jobs, locked_event, project, h.now, main_round=True
        )
    async with h.db() as session:
        assert (await session.get_one(models.Project, team_a.id)).team_status == "locked"
        assert (await session.get_one(models.SwapRequest, swap.id)).status == "closed"
        assert (await session.get_one(models.MergeProposal, merge_id)).status == "closed"
    swap_results = await results(h, "swap_result")
    merge_results = await results(h, "merge_result")
    assert {row.user_id for row in swap_results} == {lead_a.id, lead_b.id, a1.id, b1.id}
    assert {row.user_id for row in merge_results} == {lead_a.id, lead_b.id}
    assert all(payload(row)["reason"] == "The team locked" for row in swap_results + merge_results)
    async with h.db() as session:
        locked_event = (
            await session.scalars(
                select(models.Event).where(models.Event.id == event.id).with_for_update()
            )
        ).one()
        project = await session.get_one(models.Project, team_a.id)
        assert not await formation.lock_team(
            session, h.jobs, locked_event, project, h.now, main_round=True
        )
        assert (
            await formation.close_swaps_and_merges(
                session, project, locked_event, h.now, reason="The team locked"
            )
            == 0
        )
    assert {row.id for row in await results(h, "swap_result")} == {row.id for row in swap_results}
    assert {row.id for row in await results(h, "merge_result")} == {row.id for row in merge_results}


async def test_disband_closes_negotiations(harness: Harness) -> None:
    h = harness
    event, team_a, team_b, lead_a, lead_b, a1, _, b1 = await two_teams(h)
    async with h.db() as session:
        await session.execute(
            update(models.ProjectMember)
            .where(models.ProjectMember.project_id == team_a.id)
            .values(left_at=h.now, left_reason="left")
        )
        await session.execute(
            update(models.Project).where(models.Project.id == team_a.id).values(lead_user_id=None)
        )
    await add_member(
        h, event, team_a, a1, state="proposed", confirm_by=h.now, confirm_kind="placement"
    )
    async with h.db() as session:
        swap = models.SwapRequest(
            event_id=event.id,
            from_team_id=team_a.id,
            to_team_id=team_b.id,
            from_user_id=a1.id,
            to_user_id=b1.id,
            requested_by=lead_a.id,
            status="pending",
        )
        merge = models.MergeProposal(
            event_id=event.id,
            absorbed_team_id=team_a.id,
            target_team_id=team_b.id,
            proposed_by="system",
            status="pending",
        )
        session.add_all([swap, merge])
        await session.flush()
        swap_id, merge_id = swap.id, merge.id
        locked_event = (
            await session.scalars(
                select(models.Event).where(models.Event.id == event.id).with_for_update()
            )
        ).one()
        project = await session.get_one(models.Project, team_a.id)
        assert not await formation.lock_team(
            session, h.jobs, locked_event, project, h.now, main_round=True
        )
    async with h.db() as session:
        assert (await session.get_one(models.Project, team_a.id)).team_status == "disbanded"
        assert (await session.get_one(models.SwapRequest, swap_id)).status == "closed"
        assert (await session.get_one(models.MergeProposal, merge_id)).status == "closed"
    swap_results = await results(h, "swap_result")
    merge_results = await results(h, "merge_result")
    assert {row.user_id for row in swap_results} == {a1.id, b1.id, lead_b.id}
    assert {row.user_id for row in merge_results} == {lead_b.id}
    assert all(
        payload(row)["reason"] == "The team was disbanded" for row in swap_results + merge_results
    )
    assert await results(h, "team_locked") == []


@pytest.mark.parametrize("equal_deadlines", [False, True])
async def test_bounded_sweep_across_kinds(harness: Harness, equal_deadlines: bool) -> None:
    h = harness
    event, team_a, team_b, lead_a, _, a1, _, b1 = await two_teams(
        h, team_a_ends=h.now + timedelta(hours=2), team_b_ends=h.now + timedelta(hours=5)
    )
    third = await seed_project(
        h, event, name="Third", review_ends_at=h.now + timedelta(hours=2 if equal_deadlines else 1)
    )
    future = await seed_project(h, event, name="Future", review_ends_at=h.now + timedelta(hours=5))
    extra = await h.user()
    await enroll(h, event, extra)
    await add_member(h, event, third, extra)
    async with h.db() as session:
        swap = models.SwapRequest(
            id=UUID(int=2),
            event_id=event.id,
            from_team_id=team_a.id,
            to_team_id=team_b.id,
            from_user_id=a1.id,
            to_user_id=b1.id,
            requested_by=lead_a.id,
            status="pending",
            created_at=h.now - timedelta(hours=3),
        )
        merge = models.MergeProposal(
            id=UUID(int=1),
            event_id=event.id,
            absorbed_team_id=third.id,
            target_team_id=team_b.id,
            proposed_by="system",
            status="pending",
            created_at=h.now - timedelta(hours=2),
        )
        not_due = models.MergeProposal(
            id=UUID(int=3),
            event_id=event.id,
            absorbed_team_id=future.id,
            target_team_id=team_b.id,
            proposed_by="system",
            status="pending",
            created_at=h.now - timedelta(hours=4),
        )
        session.add_all([swap, merge, not_due])
    h.advance(timedelta(hours=2))
    assert await sweep(h, event, limit=1) == 1
    async with h.db() as session:
        assert (await session.get_one(models.MergeProposal, UUID(int=1))).status == "expired"
        assert (await session.get_one(models.SwapRequest, UUID(int=2))).status == "pending"
        assert (await session.get_one(models.MergeProposal, UUID(int=3))).status == "pending"
    assert await sweep(h, event, limit=1) == 1
    async with h.db() as session:
        assert (await session.get_one(models.SwapRequest, UUID(int=2))).status == "expired"
        assert (await session.get_one(models.MergeProposal, UUID(int=3))).status == "pending"
    assert await sweep(h, event, limit=1) == 0
