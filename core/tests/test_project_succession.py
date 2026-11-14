from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from harness import Harness
from hmm_contracts.common import AreaImportance, ErrorCode, EventKind, EventOrigin, EventStatus
from hmm_core import models
from hmm_core.catalog import service as catalog
from hmm_core.projects import service as projects
from sqlalchemy import func, select, update
from test_api_skeleton import assert_problem


async def seed_project(
    h: Harness,
    lead: models.User,
    *,
    members: Sequence[models.User] = (),
    name: str = "Project",
    visibility: str = "public",
    status: str = "idea",
    event_id: Any = None,
    area_keys: Sequence[str] = ("backend",),
    recruiting: dict[str, Any] | None = None,
    team_status: str | None = None,
) -> models.Project:
    async with h.db() as session:
        project = models.Project(
            event_id=event_id,
            name=name,
            description_md="",
            goal="",
            links=[],
            visibility=visibility,
            recruiting=recruiting,
            lead_user_id=lead.id,
            status=status,
            team_status=team_status,
            origin="standalone" if team_status is None else "claim",
            last_lead_action_at=h.now,
            last_progress_at=h.now,
        )
        session.add(project)
        await session.flush()
        for key in area_keys:
            area_id = await session.scalar(select(models.Area.id).where(models.Area.key == key))
            assert area_id is not None
            session.add(models.ProjectArea(project_id=project.id, area_id=area_id))
        for user in (lead, *members):
            is_lead = user.id == lead.id
            session.add(
                models.ProjectMember(
                    project_id=project.id,
                    event_id=event_id if team_status is not None else None,
                    user_id=user.id,
                    role="lead" if is_lead else "member",
                    state="confirmed",
                    trust="self",
                    joined_via="create",
                    is_team_row=False,
                    joined_at=h.now,
                    confirmed_at=h.now,
                )
            )
        await session.flush()
        await session.refresh(project)
        return project


async def confirm(
    h: Harness,
    project: models.Project,
    member: models.ProjectMember,
    confirmer: models.User,
    *,
    ignored: bool = False,
) -> models.Confirmation:
    assert member.project_id == project.id
    async with h.db() as session:
        confirmation = models.Confirmation(
            fact_type="project_member",
            fact_id=member.id,
            subject_user_id=member.user_id,
            confirmer_id=confirmer.id,
            source="peer",
            ignored=ignored,
        )
        session.add(confirmation)
        await session.flush()
        return confirmation


async def member_row(
    h: Harness, project: models.Project, user: models.User
) -> models.ProjectMember:
    async with h.db() as session:
        return (
            await session.scalars(
                select(models.ProjectMember).where(
                    models.ProjectMember.project_id == project.id,
                    models.ProjectMember.user_id == user.id,
                )
            )
        ).one()


async def clear_lead(h: Harness, project: models.Project) -> None:
    async with h.db() as session:
        await session.execute(
            update(models.Project).where(models.Project.id == project.id).values(lead_user_id=None)
        )
        await session.execute(
            update(models.ProjectMember)
            .where(models.ProjectMember.project_id == project.id)
            .values(role="member")
        )


async def succession(h: Harness, project: models.Project, *, exclude: set[UUID]) -> None:
    async with h.db() as session:
        row = await session.get_one(models.Project, project.id)
        await projects.standalone_lead_succession(session, row, h.now, exclude=exclude)


async def pending_offers(h: Harness, project: models.Project) -> list[models.LeadOffer]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.LeadOffer).where(
                    models.LeadOffer.project_id == project.id, models.LeadOffer.status == "pending"
                )
            )
        )


async def make_hackathon(h: Harness) -> models.Event:
    async with h.db() as session:
        return await catalog.create_event(
            session,
            catalog.EventInput(
                kind=EventKind.HACKATHON,
                title="Hack",
                starts_at=h.now + timedelta(days=30),
                ends_at=h.now + timedelta(days=32),
                timezone="UTC",
                areas=[catalog.EventAreaInput(key="backend", importance=AreaImportance.PRIMARY)],
            ),
            origin=EventOrigin.MANUAL,
            status=EventStatus.PUBLISHED,
            created_by=None,
            now=h.now,
        )


async def seed_offer(
    h: Harness,
    project: models.Project,
    user: models.User,
    *,
    source: str = "succession",
    expires_at: datetime | None = None,
) -> models.LeadOffer:
    async with h.db() as session:
        offer = models.LeadOffer(
            project_id=project.id,
            event_id=project.event_id,
            user_id=user.id,
            source=source,
            offered_by=project.lead_user_id if source == "transfer" else None,
            reason="Take over",
            status="pending",
            expires_at=expires_at or h.now + timedelta(days=3),
        )
        session.add(offer)
        await session.flush()
        return offer


async def test_standalone_lead_order(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    lead, m1, m2, m3, m4, left, banned = [await h.user() for _ in range(7)]
    project = await seed_project(h, lead, members=[m1, m2, m3, m4, left, banned])
    r1, r2, r3 = [await member_row(h, project, user) for user in (m1, m2, m3)]
    await confirm(h, project, r1, lead)
    await confirm(h, project, r1, m2)
    await confirm(h, project, r2, lead)
    await confirm(h, project, r3, lead, ignored=True)
    withdrawn = await confirm(h, project, r3, m1)
    async with h.db() as session:
        await session.execute(
            update(models.ProjectMember)
            .where(
                models.ProjectMember.project_id == project.id,
                models.ProjectMember.user_id.in_([lead.id, left.id]),
            )
            .values(left_at=h.now)
        )
        await session.execute(
            update(models.User).where(models.User.id == banned.id).values(banned_at=h.now)
        )
        await session.execute(
            update(models.ProjectMember)
            .where(models.ProjectMember.id == r3.id)
            .values(joined_at=h.now + timedelta(seconds=1))
        )
        await session.execute(
            update(models.Confirmation)
            .where(models.Confirmation.id == withdrawn.id)
            .values(withdrawn_at=h.now)
        )
        session.add(
            models.Confirmation(
                fact_type="project_member",
                fact_id=r3.id,
                subject_user_id=m3.id,
                confirmer_id=None,
                source="system",
                ignored=False,
            )
        )
    async with h.db() as session:
        row = await session.get_one(models.Project, project.id)
        candidates = await projects.standalone_lead_order(session, row)
        assert [candidate.user.id for candidate in candidates] == [m1.id, m2.id, m4.id, m3.id]
        assert [candidate.contributions for candidate in candidates] == [2, 1, 0, 0]


async def test_standalone_succession_offer(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    a, b, confirmer = [await h.user() for _ in range(3)]
    project = await seed_project(h, a, members=[b], status="active")
    await clear_lead(h, project)
    await confirm(h, project, await member_row(h, project, a), confirmer)
    b_row = await member_row(h, project, b)
    await confirm(h, project, b_row, a)
    await confirm(h, project, b_row, confirmer)
    await succession(h, project, exclude=set())
    [offer] = await pending_offers(h, project)
    assert offer.user_id == b.id
    assert offer.source == "succession"
    assert offer.offered_by is None
    assert offer.tier is None
    assert offer.event_id is None
    assert offer.expires_at == h.now + timedelta(days=3)
    assert offer.reason
    async with h.db() as session:
        row = await session.get_one(models.Project, project.id)
        assert row.status == "active"
        [notice] = list(await session.scalars(select(models.Notification)))
        assert notice.type == "project_lead_offer"
        assert notice.user_id == b.id
        assert isinstance(notice.payload, dict)
        assert notice.payload["source"] == "succession"
        assert notice.payload["project_name"] == project.name
        assert notice.payload["offered_by"] is None
        assert datetime.fromisoformat(str(notice.payload["expires_at"])) == offer.expires_at
        assert await session.scalar(select(func.count()).select_from(models.AuditLog)) == 0
    assert not [job for job in await h.pending_jobs() if job["status"] == "todo"]


async def test_standalone_succession_exclusions(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    a, b, c, confirmer = [await h.user() for _ in range(4)]
    project = await seed_project(h, a, members=[b, c])
    await clear_lead(h, project)
    await confirm(h, project, await member_row(h, project, a), confirmer)
    b_row = await member_row(h, project, b)
    await confirm(h, project, b_row, a)
    await confirm(h, project, b_row, confirmer)
    for exclude, expected, exclusions in (
        (set(), b.id, []),
        ({b.id}, a.id, [b.id]),
        ({a.id}, c.id, sorted([a.id, b.id], key=str)),
    ):
        await succession(h, project, exclude=exclude)
        [offer] = await pending_offers(h, project)
        assert offer.user_id == expected
        async with h.db() as session:
            row = await session.get_one(models.Project, project.id)
            assert row.lead_succession_excluded == exclusions


async def test_standalone_succession_exhaustion(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    a, b = await h.user(), await h.user()
    project = await seed_project(h, a, members=[b], status="active")
    await clear_lead(h, project)
    await succession(h, project, exclude={a.id, b.id})
    await succession(h, project, exclude={a.id, b.id})
    assert await pending_offers(h, project) == []
    async with h.db() as session:
        row = await session.get_one(models.Project, project.id)
        assert row.status == "paused"
        assert row.lead_user_id is None
        notices = list(await session.scalars(select(models.Notification)))
        assert len(notices) == 2
        assert {notice.user_id for notice in notices} == {a.id, b.id}
        for notice in notices:
            assert notice.type == "project_lead_claimable"
            assert isinstance(notice.payload, dict)
            assert notice.payload["member_count"] == 2
            assert notice.payload["reason"]
    empty = await seed_project(h, a, name="Empty")
    await clear_lead(h, empty)
    async with h.db() as session:
        await session.execute(
            update(models.ProjectMember)
            .where(models.ProjectMember.project_id == empty.id)
            .values(left_at=h.now)
        )
    await succession(h, empty, exclude=set())
    async with h.db() as session:
        assert (await session.get_one(models.Project, empty.id)).status == "paused"
        assert await session.scalar(select(func.count()).select_from(models.Notification)) == 2


async def test_standalone_succession_keeps_live_lead(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    a, b = await h.user(), await h.user()
    project = await seed_project(h, a, members=[b])
    await succession(h, project, exclude={a.id})
    assert await pending_offers(h, project) == []
    async with h.db() as session:
        row = await session.get_one(models.Project, project.id)
        assert row.lead_user_id == a.id
        assert row.lead_succession_excluded == []


async def test_project_offer_expiry_advances(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    a, b = await h.user(), await h.user()
    project = await seed_project(h, a, members=[b])
    await clear_lead(h, project)
    old = await seed_offer(h, project, a, expires_at=h.now - timedelta(minutes=1))
    async with h.db() as session:
        assert await projects.expire_project_lead_offers(session, h.now) == 1
        assert await projects.expire_project_lead_offers(session, h.now) == 0
    [offer] = await pending_offers(h, project)
    assert offer.user_id == b.id
    assert offer.source == "succession"
    async with h.db() as session:
        row = await session.get_one(models.LeadOffer, old.id)
        assert row.status == "expired"
        assert row.responded_at == h.now
        assert (await session.get_one(models.Project, project.id)).lead_succession_excluded == [
            a.id
        ]


async def test_project_offer_expiry_scopes(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    a, b = await h.user(), await h.user()
    transfer_project = await seed_project(h, a, members=[b])
    transfer = await seed_offer(h, transfer_project, b, source="transfer", expires_at=h.now)
    future_project = await seed_project(h, a, members=[b])
    future = await seed_offer(h, future_project, b, expires_at=h.now + timedelta(hours=1))
    event = await make_hackathon(h)
    team = await seed_project(h, a, members=[b], event_id=event.id, team_status="review")
    team_offer = await seed_offer(h, team, b, expires_at=h.now)
    exhausted = await seed_project(h, b)
    await clear_lead(h, exhausted)
    final_offer = await seed_offer(h, exhausted, b, expires_at=h.now)
    async with h.db() as session:
        assert await projects.expire_project_lead_offers(session, h.now) == 2
        assert (await session.get_one(models.LeadOffer, transfer.id)).status == "expired"
        assert (await session.get_one(models.Project, transfer_project.id)).lead_user_id == a.id
        assert (await session.get_one(models.LeadOffer, future.id)).status == "pending"
        assert (await session.get_one(models.LeadOffer, team_offer.id)).status == "pending"
        assert (await session.get_one(models.LeadOffer, final_offer.id)).status == "expired"
        assert (await session.get_one(models.Project, exhausted.id)).status == "paused"
        [notice] = list(await session.scalars(select(models.Notification)))
        assert notice.type == "project_lead_claimable"
        assert notice.user_id == b.id
    assert await pending_offers(h, transfer_project) == []


async def test_project_offer_expiry_limit(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    a, b = await h.user(), await h.user()
    first = await seed_project(h, a, members=[b])
    second = await seed_project(h, a, members=[b])
    older = await seed_offer(h, first, b, source="transfer", expires_at=h.now - timedelta(hours=1))
    newer = await seed_offer(h, second, b, source="transfer", expires_at=h.now)
    async with h.db() as session:
        assert await projects.expire_project_lead_offers(session, h.now, limit=1) == 1
        assert (await session.get_one(models.LeadOffer, older.id)).status == "expired"
        assert (await session.get_one(models.LeadOffer, newer.id)).status == "pending"
    async with h.db() as session:
        assert await projects.expire_project_lead_offers(session, h.now, limit=1) == 1
        assert (await session.get_one(models.LeadOffer, newer.id)).status == "expired"
        assert await projects.expire_project_lead_offers(session, h.now, limit=1) == 0


async def test_project_transfer_policy(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    a, b = await h.user(), await h.user()
    project = await seed_project(h, a, members=[b])
    async with await h.client(a) as client:
        response = await client.post(
            f"/api/v1/projects/{project.id}/transfer-lead", json={"user_id": str(b.id)}
        )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["source"] == "transfer"
    assert body["offered_by"] == str(a.id)
    assert datetime.fromisoformat(body["expires_at"]) == h.now + timedelta(days=7)
    async with h.db() as session:
        [notice] = list(await session.scalars(select(models.Notification)))
        assert notice.user_id == b.id
        assert notice.type == "project_lead_offer"
        assert isinstance(notice.payload, dict)
        assert notice.payload["offered_by"] == {
            "user_id": str(a.id),
            "display_name": a.display_name,
        }
    async with await h.client(b) as client:
        response = await client.post(f"/api/v1/lead-offers/{body['id']}/accept")
    assert response.status_code == 200, response.text
    async with h.db() as session:
        assert (await session.get_one(models.Project, project.id)).lead_user_id == b.id
        assert await session.scalar(select(func.count()).select_from(models.Notification)) == 1
    assert (await member_row(h, project, a)).role == "member"


@pytest.mark.parametrize("source", ["transfer", "succession"])
@pytest.mark.parametrize("team_status", [None, "finished", "withdrawn"])
async def test_project_transfer_accept_lead_limit(
    harness: Harness, source: str, team_status: str | None
) -> None:
    h = harness
    await h.seed_areas()
    a, b = await h.user(), await h.user()
    event = await make_hackathon(h) if team_status else None
    project = await seed_project(
        h, a, members=[b], event_id=event.id if event else None, team_status=team_status
    )
    if source == "succession":
        await clear_lead(h, project)
    offer = await seed_offer(h, project, b, source=source)
    for index in range(5):
        await seed_project(h, b, name=f"Led {index}")
    async with await h.client(b) as client:
        assert_problem(
            await client.post(f"/api/v1/lead-offers/{offer.id}/accept"),
            409,
            ErrorCode.PROJECTS_LED_LIMIT,
        )
    async with h.db() as session:
        row = await session.get_one(models.Project, project.id)
        assert row.lead_user_id == (a.id if source == "transfer" else None)
        assert (await session.get_one(models.LeadOffer, offer.id)).status == "pending"
    assert (await member_row(h, project, b)).role == "member"


async def test_project_lead_full_cycle(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    a, b, c = await h.user(), await h.user(), await h.user()
    project = await seed_project(h, a, members=[b, c], status="active")
    async with h.db() as session:
        await session.execute(
            update(models.ProjectMember)
            .where(
                models.ProjectMember.project_id == project.id, models.ProjectMember.user_id == c.id
            )
            .values(joined_at=h.now + timedelta(seconds=1))
        )
    async with await h.client(a) as client:
        response = await client.post(f"/api/v1/projects/{project.id}/leave")
    assert response.status_code == 204, response.text
    for user in (b, c):
        [offer] = await pending_offers(h, project)
        assert offer.user_id == user.id
        async with await h.client(user) as client:
            response = await client.post(f"/api/v1/lead-offers/{offer.id}/decline")
        assert response.status_code == 200, response.text
    assert await pending_offers(h, project) == []
    async with h.db() as session:
        row = await session.get_one(models.Project, project.id)
        assert row.status == "paused"
        assert row.lead_user_id is None
        notices = list(
            await session.scalars(
                select(models.Notification).where(
                    models.Notification.type == "project_lead_claimable"
                )
            )
        )
        assert len(notices) == 2
        assert {notice.user_id for notice in notices} == {b.id, c.id}
    async with await h.client(b) as client:
        response = await client.post(f"/api/v1/projects/{project.id}/claim-lead")
    assert response.status_code == 200, response.text
    assert response.json()["lead_user_id"] == str(b.id)
    async with h.db() as session:
        assert (await session.get_one(models.Project, project.id)).lead_succession_excluded == []


async def test_ban_standalone_lead(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    admin = await h.admin()
    a, b, c = await h.user(), await h.user(), await h.user()
    project = await seed_project(h, a, members=[b, c], status="active")
    a_row = await member_row(h, project, a)
    b_row = await member_row(h, project, b)
    b_confirmation = await confirm(h, project, b_row, c)
    a_confirmation = await confirm(h, project, a_row, b)
    async with h.db() as session:
        member_count = await session.scalar(select(func.count()).select_from(models.ProjectMember))
        confirmation_count = await session.scalar(
            select(func.count()).select_from(models.Confirmation)
        )
    async with await h.client(admin) as client:
        response = await client.post(f"/api/v1/admin/users/{a.id}/ban", json={"reason": "fraud"})
    assert response.status_code == 200, response.text
    assert response.json()["banned_at"] is not None
    removed = await member_row(h, project, a)
    assert removed.id == a_row.id
    assert removed.left_at == h.now
    assert removed.left_reason == "banned"
    assert removed.state == a_row.state
    assert removed.confirmed_at == a_row.confirmed_at
    [offer] = await pending_offers(h, project)
    assert offer.user_id == b.id
    assert offer.source == "succession"
    assert offer.expires_at == h.now + timedelta(days=3)
    async with h.db() as session:
        row = await session.get_one(models.Project, project.id)
        assert row.lead_user_id is None
        assert (
            await session.scalar(select(func.count()).select_from(models.ProjectMember))
            == member_count
        )
        assert (
            await session.scalar(select(func.count()).select_from(models.Confirmation))
            == confirmation_count
        )
        for original in (a_confirmation, b_confirmation):
            confirmation = await session.get_one(models.Confirmation, original.id)
            assert confirmation.fact_id == original.fact_id
            assert confirmation.ignored == original.ignored
            assert confirmation.withdrawn_at == original.withdrawn_at
        notices = list(await session.scalars(select(models.Notification)))
        offered = [notice for notice in notices if notice.type == "project_lead_offer"]
        assert len(offered) == 1
        assert offered[0].user_id == b.id
        left = [notice for notice in notices if notice.type == "project_member_left"]
        assert len(left) == 2
        assert {notice.user_id for notice in left} == {b.id, c.id}
        assert all(notice.user_id != a.id for notice in notices)
        for notice in left:
            assert isinstance(notice.payload, dict)
            assert notice.payload["member"] == {
                "user_id": str(a.id),
                "display_name": a.display_name,
            }
            assert notice.payload["member_count"] == 2
        [audit] = list(
            await session.scalars(
                select(models.AuditLog).where(models.AuditLog.action == "lead_step_down")
            )
        )
        assert audit.actor_id == admin.id
        assert audit.actor_kind == "admin"
        assert audit.entity_id == str(project.id)
        assert audit.reason == "banned"
    assert not [job for job in await h.pending_jobs() if job["status"] == "todo"]


async def test_ban_standalone_member(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    admin = await h.admin()
    a, b, c = await h.user(), await h.user(), await h.user()
    project = await seed_project(h, a, members=[b, c])
    confirmation = await confirm(h, project, await member_row(h, project, b), c)
    async with await h.client(admin) as client:
        response = await client.post(f"/api/v1/admin/users/{b.id}/ban", json={"reason": "fraud"})
    assert response.status_code == 200, response.text
    removed = await member_row(h, project, b)
    assert removed.left_reason == "banned"
    assert removed.left_at == h.now
    assert await pending_offers(h, project) == []
    async with h.db() as session:
        assert (await session.get_one(models.Project, project.id)).lead_user_id == a.id
        assert (
            await session.scalar(
                select(func.count())
                .select_from(models.AuditLog)
                .where(models.AuditLog.action == "lead_step_down")
            )
            == 0
        )
        row = await session.get_one(models.Confirmation, confirmation.id)
        assert row.ignored is False
        assert row.withdrawn_at is None
        assert await session.scalar(select(func.count()).select_from(models.Confirmation)) == 1
        notices = list(await session.scalars(select(models.Notification)))
        assert len(notices) == 2
        assert {notice.user_id for notice in notices} == {a.id, c.id}
        assert {notice.type for notice in notices} == {"project_member_left"}
    assert not [job for job in await h.pending_jobs() if job["status"] == "todo"]


async def test_ban_sole_standalone_lead(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    admin = await h.admin()
    a = await h.user()
    project = await seed_project(h, a)
    async with await h.client(admin) as client:
        response = await client.post(f"/api/v1/admin/users/{a.id}/ban", json={"reason": "fraud"})
    assert response.status_code == 200, response.text
    assert await pending_offers(h, project) == []
    async with h.db() as session:
        row = await session.get_one(models.Project, project.id)
        assert row.lead_user_id is None
        assert row.status == "paused"
        assert await session.scalar(select(func.count()).select_from(models.Notification)) == 0


async def test_ban_standalone_succession_candidate(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    admin = await h.admin()
    a, b = await h.user(), await h.user()
    project = await seed_project(h, a, members=[b], status="active")
    await clear_lead(h, project)
    old = await seed_offer(h, project, a, expires_at=h.now + timedelta(days=2))
    async with await h.client(admin) as client:
        response = await client.post(f"/api/v1/admin/users/{a.id}/ban", json={"reason": "fraud"})
    assert response.status_code == 200, response.text
    [offer] = await pending_offers(h, project)
    assert offer.user_id == b.id
    assert offer.source == "succession"
    async with h.db() as session:
        previous = await session.get_one(models.LeadOffer, old.id)
        assert previous.status == "closed"
        assert previous.responded_at == h.now
        assert (await session.get_one(models.Project, project.id)).status == "active"
        notices = list(
            await session.scalars(
                select(models.Notification).where(models.Notification.type == "project_lead_offer")
            )
        )
        assert len(notices) == 1
        assert notices[0].user_id == b.id


async def test_ban_keeps_active_team_membership(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    admin = await h.admin()
    a, b, c = await h.user(), await h.user(), await h.user()
    active_event = await make_hackathon(h)
    async with h.db() as session:
        forming_event = await catalog.create_event(
            session,
            catalog.EventInput(
                kind=EventKind.HACKATHON,
                title="Other Hack",
                starts_at=h.now + timedelta(days=35),
                ends_at=h.now + timedelta(days=37),
                timezone="UTC",
                areas=[catalog.EventAreaInput(key="backend", importance=AreaImportance.PRIMARY)],
            ),
            origin=EventOrigin.MANUAL,
            status=EventStatus.PUBLISHED,
            created_by=None,
            now=h.now,
        )
    active = await seed_project(h, a, members=[b], event_id=active_event.id, team_status="active")
    forming = await seed_project(
        h, c, members=[b], event_id=forming_event.id, team_status="forming"
    )
    async with h.db() as session:
        await session.execute(
            update(models.ProjectMember)
            .where(models.ProjectMember.project_id.in_([active.id, forming.id]))
            .values(is_team_row=True)
        )
    async with await h.client(admin) as client:
        response = await client.post(f"/api/v1/admin/users/{b.id}/ban", json={"reason": "fraud"})
    assert response.status_code == 200, response.text
    assert (await member_row(h, active, b)).left_at is None
    assert (await member_row(h, forming, b)).left_reason == "banned"
    async with h.db() as session:
        assert (await session.get_one(models.Project, active.id)).lead_user_id == a.id
