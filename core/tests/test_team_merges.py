import asyncio
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from harness import Harness
from hmm_contracts.common import (
    AuditAction,
    ErrorCode,
    EventStatus,
    FormationPhase,
    JoinedVia,
    LeftReason,
    MemberRole,
    MemberState,
    MergeProposalStatus,
    MergeProposedBy,
    ProjectStatus,
    TeamStatus,
)
from hmm_contracts.notifications import AlertKind
from hmm_core import models
from hmm_core.formation import service as formation
from hmm_core.notifications.service import raise_alert
from sqlalchemy import func, select, update
from test_api_skeleton import assert_problem
from test_placement_confirmation import add_member, enroll, make_hackathon, seed_project
from test_swaps import two_teams


async def merge_teams(
    h: Harness,
    *,
    absorbed_members: int = 2,
    target_members: int = 1,
    team_size_max: int | None = 6,
) -> tuple[
    models.Event, models.Project, models.Project, models.User, models.User, list[models.User]
]:
    await h.seed_areas()
    event = await make_hackathon(h, team_size_max=team_size_max, team_size_min=2)
    absorbed = await seed_project(
        h, event, name="Absorbed", review_ends_at=h.now + timedelta(days=1)
    )
    target = await seed_project(h, event, name="Target", review_ends_at=h.now + timedelta(days=1))
    absorbed_lead, target_lead = await h.user(), await h.user()
    absorbed_users = [await h.user() for _ in range(absorbed_members)]
    target_users = [await h.user() for _ in range(target_members)]
    for project, lead, users in (
        (absorbed, absorbed_lead, absorbed_users),
        (target, target_lead, target_users),
    ):
        await enroll(h, event, lead, status="placed")
        await add_member(h, event, project, lead, role="lead", set_lead=True)
        for user in users:
            await enroll(h, event, user, status="placed")
            await add_member(h, event, project, user)
    return event, absorbed, target, absorbed_lead, target_lead, absorbed_users


async def propose_merge(
    h: Harness,
    absorbed: models.Project,
    target: models.Project,
    actor: models.User,
) -> dict[str, Any]:
    async with await h.client(actor) as client:
        response = await client.post(
            f"/api/v1/projects/{absorbed.id}/merges", json={"target_team_id": str(target.id)}
        )
    assert response.status_code == 201, response.text
    return response.json()


async def merge_notices(h: Harness, kind: str) -> list[models.Notification]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.Notification).where(models.Notification.type == kind)
            )
        )


def payload(row: models.Notification) -> dict[str, Any]:
    return cast(dict[str, Any], row.payload)


async def test_propose_merge(harness: Harness) -> None:
    h = harness
    _, absorbed, target, lead_a, lead_b, _ = await merge_teams(h)
    body = await propose_merge(h, absorbed, target, lead_a)
    assert body["status"] == "pending"
    assert body["proposed_by"] == "lead"
    assert body["run_id"] is None
    assert body["absorbed_team_id"] == str(absorbed.id)
    assert body["target_team_id"] == str(target.id)
    assert body["absorbed_accepted_at"] is None
    assert body["target_accepted_at"] is None
    assert body["allowed_actions"] == ["accept", "decline"]
    async with h.db() as session:
        merge = await session.get_one(models.MergeProposal, UUID(body["id"]))
        assert (await formation.merge_view(session, merge, lead_b, h.now)).allowed_actions == [
            "accept",
            "decline",
        ]
        assert await session.scalar(select(func.count()).select_from(models.MergeProposal)) == 1
        assert (await session.get_one(models.Project, absorbed.id)).last_lead_action_at == h.now
        assert await session.scalar(select(func.count()).select_from(models.AuditLog)) == 0
    rows = await merge_notices(h, "merge_proposed")
    assert {row.user_id for row in rows} == {lead_a.id, lead_b.id}
    for row in rows:
        data = payload(row)
        assert data["merge_proposal_id"] == body["id"]
        assert data["project_id"] == str(absorbed.id if row.user_id == lead_a.id else target.id)
        assert data["merged_size"] == 5
        assert datetime.fromisoformat(data["expires_at"]) == h.now + timedelta(days=1)
        assert data["target_lead"]["user_id"] == str(lead_b.id)
        assert len(data["members"]) == 2
        assert [action["key"] for action in data["actions"]] == ["merge_accept", "merge_decline"]
    assert not [job for job in await h.pending_jobs() if job["status"] == "todo"]


@pytest.mark.parametrize("mode", ["lead", "system", "concurrent"])
async def test_apply_merge(harness: Harness, mode: str) -> None:
    h = harness
    event, absorbed, target, lead_a, lead_b, users = await merge_teams(h)
    outsider = await h.user()
    async with h.db() as session:
        area = await session.scalar(select(models.Area.id).where(models.Area.key == "backend"))
        await session.execute(
            update(models.ProjectMember)
            .where(models.ProjectMember.user_id == users[0].id)
            .values(
                state=MemberState.PROPOSED.value,
                confirm_by=h.now + timedelta(hours=5),
                confirm_kind="placement",
                area_id=area,
            )
        )
        await session.execute(
            update(models.Project).where(models.Project.id == absorbed.id).values(ready_at=h.now)
        )
        old_rows = list(
            await session.scalars(
                select(models.ProjectMember).where(models.ProjectMember.project_id == absorbed.id)
            )
        )
        invite = models.Invite(
            project_id=absorbed.id,
            event_id=event.id,
            user_id=outsider.id,
            invited_by=lead_a.id,
            kind="fill",
            status="pending",
            expires_at=h.now + timedelta(hours=5),
        )
        request = models.JoinRequest(
            project_id=absorbed.id, event_id=event.id, user_id=outsider.id, status="pending"
        )
        offer = models.LeadOffer(
            project_id=absorbed.id,
            event_id=event.id,
            user_id=users[0].id,
            source="succession",
            reason="Succession",
            status="pending",
            expires_at=h.now + timedelta(hours=5),
        )
        session.add_all([invite, request, offer])
        await session.flush()
        await session.refresh(invite)
        await session.refresh(request)
        await session.refresh(offer)
    alert_id: UUID | None = None
    if mode == "system":
        async with h.db() as session:
            run = models.FormationRun(
                event_id=event.id,
                round=1,
                kind="main",
                status="applied",
                planner="fallback",
                input={},
                output=None,
                validation_errors=[],
                algo_version="fake-1",
                applied_at=h.now,
            )
            session.add(run)
            await session.flush()
            merge = models.MergeProposal(
                event_id=event.id,
                run_id=run.id,
                absorbed_team_id=absorbed.id,
                target_team_id=target.id,
                proposed_by=MergeProposedBy.SYSTEM.value,
                status="pending",
            )
            session.add(merge)
            await session.execute(
                update(models.EventFormation)
                .where(models.EventFormation.event_id == event.id)
                .values(rounds=1)
            )
            alert = await raise_alert(
                session,
                AlertKind.FORMATION_UNDERSIZED,
                h.now,
                title="Undersized",
                message="Merge needed",
                link=f"/projects/{absorbed.id}",
                subject_type="project",
                subject_id=absorbed.id,
                dedupe_key=f"formation_undersized:{absorbed.id}:1",
            )
            assert alert is not None
            await session.flush()
            merge_id, run_id, alert_id = merge.id, run.id, alert.id
    else:
        body = await propose_merge(h, absorbed, target, lead_a)
        merge_id, run_id = UUID(body["id"]), None

    async def accept(actor: models.User) -> dict[str, Any]:
        async with await h.client(actor) as client:
            response = await client.post(f"/api/v1/merges/{merge_id}/accept")
        assert response.status_code == 200, response.text
        return response.json()

    if mode == "concurrent":
        responses = await asyncio.gather(accept(lead_a), accept(lead_b))
        assert sorted(response["status"] for response in responses) == ["accepted", "pending"]
        final = next(response for response in responses if response["status"] == "accepted")
    else:
        assert (await accept(lead_b))["status"] == "pending"
        final = await accept(lead_a)
    assert final["status"] == "accepted"
    assert final["run_id"] == (str(run_id) if run_id is not None else None)
    assert final["allowed_actions"] == []
    assert await accept(lead_b) == final
    rows = await merge_notices(h, "merge_result")
    assert len(rows) == 5
    assert all(
        payload(row)["status"] == "accepted" and payload(row)["merged_size"] == 5 for row in rows
    )
    jobs = [
        job for job in await h.pending_jobs("refresh_recommendations") if job["status"] == "todo"
    ]
    assert len(jobs) == 1
    assert jobs[0]["args"]["project_id"] == str(target.id)
    async with h.db() as session:
        assert await session.scalar(select(func.count()).select_from(models.ProjectMember)) == 8
        live = list(
            await session.scalars(
                select(models.ProjectMember).where(models.ProjectMember.left_at.is_(None))
            )
        )
        assert len(live) == 5
        assert {row.user_id for row in live} == {row.user_id for row in rows}
        assert all(row.project_id == target.id for row in live)
        for before in old_rows:
            closed = await session.get_one(models.ProjectMember, before.id)
            assert closed.left_at == h.now
            assert closed.left_reason == LeftReason.MERGED.value
            after = next(row for row in live if row.user_id == before.user_id)
            assert after.joined_via == JoinedVia.MERGE.value
            assert after.role == MemberRole.MEMBER.value
            for field in (
                "state",
                "confirm_by",
                "confirm_kind",
                "confirmed_at",
                "area_id",
                "trust",
                "proposal_reason",
            ):
                assert getattr(after, field) == getattr(before, field)
        assert (
            next(row for row in live if row.user_id == lead_a.id).state
            == MemberState.CONFIRMED.value
        )
        absorbed_row = await session.get_one(models.Project, absorbed.id)
        assert absorbed_row.team_status == TeamStatus.DISBANDED.value
        assert absorbed_row.status == ProjectStatus.DISBANDED.value
        assert absorbed_row.lead_user_id is None
        assert absorbed_row.ready_at is None
        assert absorbed_row.last_lead_action_at == h.now
        assert (await session.get_one(models.Project, target.id)).lead_user_id == lead_b.id
        assert (
            await session.scalar(
                select(func.count())
                .select_from(models.Project)
                .where(models.Project.team_status == "disbanded")
            )
            == 1
        )
        audits = list(await session.scalars(select(models.AuditLog)))
        assert len(audits) == 2
        assert {row.action for row in audits} == {
            AuditAction.LEAD_STEP_DOWN.value,
            AuditAction.SYSTEM_TRANSITION.value,
        }
        assert all(
            row.actor_id is None
            and row.actor_kind == "system"
            and row.entity_id == str(absorbed.id)
            for row in audits
        )
        assert (await session.get_one(models.Invite, invite.id)).status == "closed"
        assert (await session.get_one(models.Invite, invite.id)).closed_reason == "team_disbanded"
        assert (await session.get_one(models.JoinRequest, request.id)).status == "closed"
        assert (await session.get_one(models.LeadOffer, offer.id)).status in {"cancelled", "closed"}
        if alert_id is not None:
            stored_alert = await session.get_one(models.AdminAlert, alert_id)
            assert stored_alert.status == "resolved"
            assert stored_alert.resolution_note == "The team was absorbed by a merge"
    async with await h.client(lead_b) as client:
        assert_problem(
            await client.post(f"/api/v1/merges/{merge_id}/decline"), 409, ErrorCode.INVALID_STATE
        )


async def test_decline_merge(harness: Harness) -> None:
    h = harness
    _, absorbed, target, lead_a, lead_b, _ = await merge_teams(h)
    body = await propose_merge(h, absorbed, target, lead_a)
    async with await h.client(lead_b) as client:
        response = await client.post(f"/api/v1/merges/{body['id']}/decline")
        assert response.status_code == 200
        assert response.json()["status"] == "declined"
        repeated = await client.post(f"/api/v1/merges/{body['id']}/decline")
        assert repeated.status_code == 200
        assert repeated.json() == response.json()
        assert_problem(
            await client.post(f"/api/v1/merges/{body['id']}/accept"), 409, ErrorCode.INVALID_STATE
        )
    rows = await merge_notices(h, "merge_result")
    assert len(rows) == 2
    assert {row.user_id for row in rows} == {lead_a.id, lead_b.id}
    assert all(payload(row)["status"] == "declined" for row in rows)
    async with h.db() as session:
        assert await session.scalar(select(func.count()).select_from(models.ProjectMember)) == 5
        assert (
            await session.scalar(
                select(func.count())
                .select_from(models.ProjectMember)
                .where(models.ProjectMember.left_at.is_not(None))
            )
            == 0
        )
        assert await session.scalar(select(func.count()).select_from(models.AuditLog)) == 0
    assert not [job for job in await h.pending_jobs() if job["status"] == "todo"]


async def test_merge_propose_capacity(harness: Harness) -> None:
    h = harness
    _, absorbed, target, lead_a, _, _ = await merge_teams(h, absorbed_members=3, target_members=2)
    async with await h.client(lead_a) as client:
        response = await client.post(
            f"/api/v1/projects/{absorbed.id}/merges", json={"target_team_id": str(target.id)}
        )
    assert_problem(response, 409, ErrorCode.MERGE_TOO_LARGE)
    async with h.db() as session:
        assert await session.scalar(select(func.count()).select_from(models.MergeProposal)) == 0


@pytest.mark.parametrize("change", ["capacity", "team", "leaderless"])
async def test_merge_apply_rechecks(harness: Harness, change: str) -> None:
    h = harness
    event, absorbed, target, lead_a, lead_b, _ = await merge_teams(h, absorbed_members=3)
    body = await propose_merge(h, absorbed, target, lead_a)
    async with await h.client(lead_b) as client:
        assert (await client.post(f"/api/v1/merges/{body['id']}/accept")).status_code == 200
    if change == "capacity":
        for _ in range(2):
            user = await h.user()
            await enroll(h, event, user)
            await add_member(h, event, absorbed, user)
    else:
        async with h.db() as session:
            await session.execute(
                update(models.Project)
                .where(models.Project.id == target.id)
                .values(
                    **({"team_status": "locked"} if change == "team" else {"lead_user_id": None})
                )
            )
    async with await h.client(lead_a) as client:
        response = await client.post(f"/api/v1/merges/{body['id']}/accept")
    assert_problem(response, 409, ErrorCode.INVALID_STATE)
    rows = await merge_notices(h, "merge_result")
    assert len(rows) == (1 if change == "leaderless" else 2)
    assert all(payload(row)["status"] == "closed" for row in rows)
    async with h.db() as session:
        assert (
            await session.get_one(models.MergeProposal, UUID(body["id"]))
        ).status == MergeProposalStatus.CLOSED.value
        assert (
            await session.scalar(
                select(func.count())
                .select_from(models.ProjectMember)
                .where(models.ProjectMember.left_at.is_not(None))
            )
            == 0
        )
        assert await session.scalar(select(func.count()).select_from(models.AuditLog)) == 0
    assert not [job for job in await h.pending_jobs() if job["status"] == "todo"]


async def test_merge_at_capacity(harness: Harness) -> None:
    h = harness
    _, absorbed, target, lead_a, lead_b, _ = await merge_teams(h, absorbed_members=3)
    body = await propose_merge(h, absorbed, target, lead_a)
    for lead in (lead_a, lead_b):
        async with await h.client(lead) as client:
            response = await client.post(f"/api/v1/merges/{body['id']}/accept")
        assert response.status_code == 200, response.text
    assert response.json()["status"] == "accepted"
    assert len(await merge_notices(h, "merge_result")) == 6


@pytest.mark.parametrize(
    "gate",
    [
        "nonlead",
        "unknown",
        "other_event",
        "same_team",
        "leaderless",
        "absorbed_locked",
        "target_forming",
        "pending_merge",
        "pending_swap",
        "forming",
        "cancelled",
        "anonymous",
    ],
)
async def test_merge_propose_gates(harness: Harness, gate: str) -> None:
    h = harness
    event, absorbed, target, lead_a, lead_b, a1, _, b1 = await two_teams(h)
    project_id, target_id = absorbed.id, target.id
    status, code = 409, ErrorCode.INVALID_STATE
    if gate == "unknown":
        project_id = uuid4()
        status, code = 404, ErrorCode.NOT_FOUND
    if gate == "other_event":
        other_event = await make_hackathon(h)
        other_team = await seed_project(h, other_event)
        target_id = other_team.id
        status, code = 404, ErrorCode.NOT_FOUND
    if gate == "same_team":
        target_id = absorbed.id
    async with h.db() as session:
        if gate == "leaderless":
            await session.execute(
                update(models.Project)
                .where(models.Project.id == target.id)
                .values(lead_user_id=None)
            )
        if gate in {"absorbed_locked", "target_forming"}:
            await session.execute(
                update(models.Project)
                .where(
                    models.Project.id == (absorbed.id if gate == "absorbed_locked" else target.id)
                )
                .values(team_status="locked" if gate == "absorbed_locked" else "forming")
            )
        if gate == "pending_merge":
            session.add(
                models.MergeProposal(
                    event_id=event.id,
                    absorbed_team_id=target.id,
                    target_team_id=absorbed.id,
                    proposed_by="system",
                    status="pending",
                )
            )
        if gate == "pending_swap":
            session.add(
                models.SwapRequest(
                    event_id=event.id,
                    from_team_id=absorbed.id,
                    to_team_id=target.id,
                    from_user_id=a1.id,
                    to_user_id=b1.id,
                    requested_by=lead_a.id,
                    status="pending",
                )
            )
        if gate == "forming":
            await session.execute(
                update(models.EventFormation)
                .where(models.EventFormation.event_id == event.id)
                .values(phase=FormationPhase.FORMING.value)
            )
            code = ErrorCode.FORMATION_IN_PROGRESS
        if gate == "cancelled":
            await session.execute(
                update(models.Event)
                .where(models.Event.id == event.id)
                .values(status=EventStatus.CANCELLED.value)
            )
    if gate == "anonymous":
        async with h.anonymous() as client:
            response = await client.post(
                f"/api/v1/projects/{project_id}/merges", json={"target_team_id": str(target_id)}
            )
        assert response.status_code == 401
        return
    if gate == "nonlead":
        status, code = 403, ErrorCode.FORBIDDEN
    async with await h.client(a1 if gate == "nonlead" else lead_a) as client:
        response = await client.post(
            f"/api/v1/projects/{project_id}/merges", json={"target_team_id": str(target_id)}
        )
    assert_problem(response, status, code)
    async with await h.client(lead_b) as client:
        assert_problem(
            await client.post(f"/api/v1/merges/{uuid4()}/accept"), 404, ErrorCode.NOT_FOUND
        )


async def test_merge_changed_lead_and_added_member(harness: Harness) -> None:
    h = harness
    event, absorbed, target, lead_a, lead_b, users = await merge_teams(h)
    body = await propose_merge(h, absorbed, target, lead_a)
    async with h.db() as session:
        await session.execute(
            update(models.Project)
            .where(models.Project.id == absorbed.id)
            .values(lead_user_id=users[0].id)
        )
    async with await h.client(lead_a) as client:
        assert_problem(
            await client.post(f"/api/v1/merges/{body['id']}/accept"), 403, ErrorCode.FORBIDDEN
        )
    added = await h.user()
    await enroll(h, event, added)
    await add_member(h, event, absorbed, added)
    for lead in (users[0], lead_b):
        async with await h.client(lead) as client:
            response = await client.post(f"/api/v1/merges/{body['id']}/accept")
        assert response.status_code == 200, response.text
    assert response.json()["status"] == "accepted"
    async with h.db() as session:
        row = await session.scalar(
            select(models.ProjectMember).where(
                models.ProjectMember.user_id == added.id, models.ProjectMember.left_at.is_(None)
            )
        )
        assert row is not None
        assert row.project_id == target.id
        assert row.joined_via == "merge"


async def test_merge_deadline(harness: Harness) -> None:
    h = harness
    _, absorbed, target, lead_a, lead_b, _, _, _ = await two_teams(
        h, team_a_ends=h.now + timedelta(hours=1), team_b_ends=h.now + timedelta(hours=3)
    )
    body = await propose_merge(h, absorbed, target, lead_a)
    assert all(
        datetime.fromisoformat(payload(row)["expires_at"]) == h.now + timedelta(hours=1)
        for row in await merge_notices(h, "merge_proposed")
    )
    h.advance(timedelta(hours=1))
    async with await h.client(lead_b) as client:
        assert_problem(
            await client.post(f"/api/v1/merges/{body['id']}/accept"), 409, ErrorCode.INVALID_STATE
        )
    async with h.db() as session:
        merge = await session.get_one(models.MergeProposal, UUID(body["id"]))
        assert merge.status == "expired"
        assert (await formation.merge_view(session, merge, lead_b, h.now)).allowed_actions == []
    rows = await merge_notices(h, "merge_result")
    assert len(rows) == 2
    assert all(payload(row)["status"] == "expired" for row in rows)
