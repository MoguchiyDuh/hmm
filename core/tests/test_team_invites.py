import asyncio
import itertools
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
from harness import Harness
from hmm_contracts.common import (
    AreaImportance,
    ErrorCode,
    EventKind,
    EventOrigin,
    EventStatus,
)
from hmm_core import models
from hmm_core.catalog import service as catalog
from sqlalchemy import delete, func, select, update
from test_api_skeleton import assert_problem

_titles = itertools.count(1)


async def make_hackathon(
    h: Harness,
    *,
    starts_in: timedelta = timedelta(days=30),
    registration_deadline: datetime | None = None,
    team_size_min: int | None = None,
    team_size_max: int | None = None,
) -> models.Event:
    async with h.db() as session:
        return await catalog.create_event(
            session,
            catalog.EventInput(
                kind=EventKind.HACKATHON,
                title=f"Hack {next(_titles)}",
                starts_at=h.now + starts_in,
                ends_at=h.now + starts_in + timedelta(days=2),
                timezone="UTC",
                team_size_min=team_size_min,
                team_size_max=team_size_max,
                registration_deadline=registration_deadline,
                areas=[catalog.EventAreaInput(key="backend", importance=AreaImportance.PRIMARY)],
            ),
            origin=EventOrigin.MANUAL,
            status=EventStatus.PUBLISHED,
            created_by=None,
            now=h.now,
        )


async def seed_team(
    h: Harness,
    event: models.Event,
    lead: models.User,
    *,
    team_status: str = "forming",
    members: Sequence[models.User] = (),
    origin: str = "claim",
    name: str = "Core",
) -> models.Project:
    async with h.db() as session:
        project = models.Project(
            event_id=event.id,
            name=name,
            description_md="",
            goal="",
            links=[],
            visibility="public",
            recruiting=None,
            lead_user_id=lead.id,
            status="idea",
            team_status=team_status,
            origin=origin,
        )
        session.add(project)
        await session.flush()
        for user in (lead, *members):
            is_lead = user.id == lead.id
            session.add(
                models.ProjectMember(
                    project_id=project.id,
                    event_id=event.id,
                    user_id=user.id,
                    role="lead" if is_lead else "member",
                    state="confirmed",
                    trust="self",
                    joined_via="claim" if is_lead else "invite",
                    is_team_row=True,
                    joined_at=h.now,
                    confirmed_at=h.now,
                )
            )
        await session.flush()
        await session.refresh(project)
        return project


async def enroll(
    h: Harness, event: models.Event, user: models.User, status: str = "active"
) -> models.Enrollment:
    async with h.db() as session:
        area_id = await session.scalar(select(models.Area.id).where(models.Area.key == "backend"))
        enrollment = models.Enrollment(
            event_id=event.id,
            user_id=user.id,
            status=status,
            source="self",
            willing_to_lead=False,
            status_at=h.now,
            preferred_area_id=area_id,
            note="hi",
        )
        session.add(enrollment)
        await session.flush()
        await session.refresh(enrollment)
        return enrollment


async def seed_invite(
    h: Harness,
    project: models.Project,
    user: models.User,
    inviter: models.User,
    *,
    kind: str = "core",
    status: str = "pending",
    expires_in: timedelta = timedelta(days=7),
) -> models.Invite:
    async with h.db() as session:
        invite = models.Invite(
            project_id=project.id,
            event_id=project.event_id,
            user_id=user.id,
            invited_by=inviter.id,
            kind=kind,
            message=None,
            area_id=None,
            status=status,
            closed_reason=None,
            expires_at=h.now + expires_in,
            responded_at=None,
        )
        session.add(invite)
        await session.flush()
        await session.refresh(invite)
        return invite


async def invite(
    h: Harness, actor: models.User, project_id: UUID, user: models.User, **body: Any
) -> httpx.Response:
    async with await h.client(actor) as c:
        return await c.post(
            f"/api/v1/projects/{project_id}/invites", json={"user_id": str(user.id), **body}
        )


async def set_formation(h: Harness, event: models.Event, **values: Any) -> None:
    async with h.db() as session:
        await session.execute(
            update(models.EventFormation)
            .where(models.EventFormation.event_id == event.id)
            .values(**values)
        )


async def set_invites_status(h: Harness, project: models.Project, status: str) -> None:
    async with h.db() as session:
        await session.execute(
            update(models.Invite)
            .where(models.Invite.project_id == project.id)
            .values(status=status)
        )


async def notifications_for(h: Harness, user: models.User, kind: str) -> list[models.Notification]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.Notification).where(
                    models.Notification.user_id == user.id, models.Notification.type == kind
                )
            )
        )


async def count(h: Harness, model: Any, *conditions: Any) -> int:
    async with h.db() as session:
        return await session.scalar(select(func.count()).select_from(model).where(*conditions)) or 0


async def seed_standalone(
    h: Harness,
    lead: models.User,
    *,
    visibility: str = "public",
    status: str = "idea",
    members: Sequence[models.User] = (),
) -> models.Project:
    async with h.db() as session:
        project = models.Project(
            event_id=None,
            name="Side",
            description_md="",
            goal="",
            links=[],
            visibility=visibility,
            recruiting=None,
            lead_user_id=lead.id,
            status=status,
            team_status=None,
            origin="standalone",
        )
        session.add(project)
        await session.flush()
        for user in (lead, *members):
            is_lead = user.id == lead.id
            session.add(
                models.ProjectMember(
                    project_id=project.id,
                    event_id=None,
                    user_id=user.id,
                    role="lead" if is_lead else "member",
                    state="confirmed",
                    trust="self",
                    joined_via="invite",
                    is_team_row=False,
                    joined_at=h.now,
                    confirmed_at=h.now,
                )
            )
        await session.flush()
        await session.refresh(project)
        return project


async def test_core_invite_created(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    lead, other, member = await h.user(), await h.user(), await h.user()
    team = await seed_team(h, event, lead, members=[other])
    await enroll(h, event, member)
    response = await invite(h, lead, team.id, member, message="join us", area_key="backend")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["kind"] == "core"
    assert body["status"] == "pending"
    assert datetime.fromisoformat(body["expires_at"]) == h.now + timedelta(days=7)
    assert body["message"] == "join us"
    assert body["area_key"] == "backend"
    assert body["project"]["id"] == str(team.id)
    assert body["user"]["id"] == str(member.id)
    assert body["invited_by"] == str(lead.id)
    assert body["inviter"]["display_name"] == lead.display_name
    assert body["responded_at"] is None
    assert body["allowed_actions"] == ["withdraw"]
    async with h.db() as session:
        rows = list(await session.scalars(select(models.Invite)))
        project = await session.get_one(models.Project, team.id)
    assert len(rows) == 1
    assert rows[0].event_id == event.id
    assert project.last_lead_action_at == h.now
    sent = await notifications_for(h, member, "invite_received")
    assert len(sent) == 1
    payload = cast(dict[str, Any], sent[0].payload)
    assert payload["kind"] == "core"
    assert payload["invite_id"] == str(rows[0].id)
    assert [action["key"] for action in payload["actions"]] == ["invite_accept", "invite_decline"]
    assert await h.pending_jobs() == []


async def test_invite_expiry_caps(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    await set_formation(h, event, formation_at=h.now + timedelta(days=2))
    lead, member = await h.user(), await h.user()
    team = await seed_team(h, event, lead)
    response = await invite(h, lead, team.id, member)
    assert response.status_code == 201, response.text
    assert datetime.fromisoformat(response.json()["expires_at"]) == h.now + timedelta(days=2)

    capped = await make_hackathon(h, registration_deadline=h.now + timedelta(days=3))
    fill_lead, fill_member = await h.user(), await h.user()
    fill = await seed_team(h, capped, fill_lead, team_status="review")
    response = await invite(h, fill_lead, fill.id, fill_member)
    assert response.status_code == 201, response.text
    assert response.json()["kind"] == "fill"
    assert datetime.fromisoformat(response.json()["expires_at"]) == h.now + timedelta(days=3)

    default = await make_hackathon(h)
    default_lead, default_member = await h.user(), await h.user()
    default_team = await seed_team(h, default, default_lead, team_status="review")
    response = await invite(h, default_lead, default_team.id, default_member)
    assert response.status_code == 201, response.text
    assert datetime.fromisoformat(response.json()["expires_at"]) == h.now + timedelta(days=7)


async def test_core_limit(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h, team_size_max=5)
    lead, a, b, c, d = [await h.user() for _ in range(5)]
    team = await seed_team(h, event, lead, members=[a])
    assert (await invite(h, lead, team.id, b)).status_code == 201
    assert_problem(await invite(h, lead, team.id, c), 409, ErrorCode.CORE_FULL)

    other_event = await make_hackathon(h, team_size_max=5)
    other_lead, x, y, z = [await h.user() for _ in range(4)]
    full = await seed_team(h, other_event, other_lead, members=[x, y])
    assert_problem(await invite(h, other_lead, full.id, z), 409, ErrorCode.CORE_FULL)

    await set_invites_status(h, team, "withdrawn")
    response = await invite(h, lead, team.id, d)
    assert response.status_code == 201, response.text


async def test_fill_limits(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h, team_size_max=5)
    lead, member, invitee = await h.user(), await h.user(), await h.user()
    team = await seed_team(h, event, lead, team_status="review", members=[member])
    response = await invite(h, lead, team.id, invitee)
    assert response.status_code == 201, response.text
    assert response.json()["kind"] == "fill"

    big_lead = await h.user()
    others = [await h.user() for _ in range(4)]
    big = await seed_team(h, event, big_lead, team_status="review", members=others, name="Big")
    assert_problem(await invite(h, big_lead, big.id, await h.user()), 409, ErrorCode.TEAM_FULL)

    late = await make_hackathon(h, registration_deadline=h.now - timedelta(minutes=1))
    late_lead = await h.user()
    locked = await seed_team(h, late, late_lead, team_status="locked")
    assert_problem(
        await invite(h, late_lead, locked.id, await h.user()), 409, ErrorCode.ENROLLMENT_CLOSED
    )


async def test_team_gates(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    lead, member, invitee = await h.user(), await h.user(), await h.user()
    team = await seed_team(h, event, lead, members=[member])

    await set_formation(h, event, phase="forming")
    assert_problem(await invite(h, lead, team.id, invitee), 409, ErrorCode.FORMATION_IN_PROGRESS)
    await set_formation(h, event, phase="locked")
    assert_problem(await invite(h, lead, team.id, invitee), 409, ErrorCode.INVALID_STATE)
    await set_formation(h, event, phase="open")

    active_lead = await h.user()
    active = await seed_team(h, event, active_lead, team_status="active", name="Active")
    assert_problem(await invite(h, active_lead, active.id, invitee), 409, ErrorCode.INVALID_STATE)

    assert_problem(await invite(h, lead, uuid4(), invitee), 404, ErrorCode.NOT_FOUND)
    assert_problem(await invite(h, member, team.id, invitee), 403, ErrorCode.FORBIDDEN)
    async with h.anonymous() as c:
        response = await c.post(
            f"/api/v1/projects/{team.id}/invites", json={"user_id": str(invitee.id)}
        )
    assert_problem(response, 401, ErrorCode.UNAUTHENTICATED)
    stranger = await h.user(onboarded=False)
    assert_problem(await invite(h, stranger, team.id, invitee), 403, ErrorCode.ONBOARDING_REQUIRED)
    assert await count(h, models.Invite) == 0


async def test_invitee_gates(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    lead, member = await h.user(), await h.user()
    team = await seed_team(h, event, lead)

    async with await h.client(lead) as c:
        response = await c.post(
            f"/api/v1/projects/{team.id}/invites", json={"user_id": str(uuid4())}
        )
    assert_problem(response, 404, ErrorCode.NOT_FOUND)
    assert_problem(
        await invite(h, lead, team.id, await h.user(onboarded=False)), 409, ErrorCode.INVALID_STATE
    )
    assert_problem(
        await invite(h, lead, team.id, await h.user(banned_at=h.now)), 409, ErrorCode.INVALID_STATE
    )
    assert_problem(
        await invite(h, lead, team.id, await h.user(dnd_indefinite=True)), 409, ErrorCode.USER_DND
    )
    taken = await h.user()
    await seed_team(h, event, await h.user(), members=[taken], name="Other")
    assert_problem(await invite(h, lead, team.id, taken), 409, ErrorCode.ALREADY_IN_TEAM)

    assert (await invite(h, lead, team.id, member)).status_code == 201
    assert_problem(await invite(h, lead, team.id, member), 409, ErrorCode.ALREADY_INVITED)
    assert await count(h, models.Invite, models.Invite.project_id == team.id) == 1
    assert len(await notifications_for(h, member, "invite_received")) == 1

    await set_invites_status(h, team, "withdrawn")
    response = await invite(h, lead, team.id, member)
    assert response.status_code == 201, response.text


async def test_standalone_invites(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    lead, invitee, existing = await h.user(), await h.user(), await h.user()
    project = await seed_standalone(h, lead, members=[existing])
    response = await invite(h, lead, project.id, invitee)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["kind"] == "project"
    assert body["event_id"] is None
    assert datetime.fromisoformat(body["expires_at"]) == h.now + timedelta(days=7)
    assert len(await notifications_for(h, invitee, "project_invite_received")) == 1
    assert_problem(await invite(h, lead, project.id, existing), 409, ErrorCode.INVALID_STATE)

    hidden = await seed_standalone(h, await h.user(), visibility="members")
    random = await h.user()
    assert_problem(await invite(h, random, hidden.id, invitee), 404, ErrorCode.NOT_FOUND)

    abandoned_lead = await h.user()
    abandoned = await seed_standalone(h, abandoned_lead, status="abandoned")
    assert_problem(
        await invite(h, abandoned_lead, abandoned.id, invitee), 409, ErrorCode.INVALID_STATE
    )


async def test_invite_lists(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    lead, member, first, second = [await h.user() for _ in range(4)]
    team = await seed_team(h, event, lead, members=[member])
    older = await seed_invite(h, team, first, lead)
    newer = await seed_invite(h, team, second, lead)

    async with await h.client(lead) as c:
        response = await c.get(f"/api/v1/projects/{team.id}/invites")
    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()] == [str(newer.id), str(older.id)]
    assert all(row["allowed_actions"] == ["withdraw"] for row in response.json())

    async with await h.client(member) as c:
        assert_problem(await c.get(f"/api/v1/projects/{team.id}/invites"), 403, ErrorCode.FORBIDDEN)
    async with await h.client(await h.admin()) as c:
        response = await c.get(f"/api/v1/projects/{team.id}/invites")
    assert response.status_code == 200, response.text
    assert len(response.json()) == 2
    assert all(row["allowed_actions"] == [] for row in response.json())

    async with await h.client(first) as c:
        mine = await c.get("/api/v1/me/invites")
        pending = await c.get("/api/v1/me/invites", params={"status": "pending"})
        accepted = await c.get("/api/v1/me/invites", params={"status": "accepted"})
    assert mine.status_code == 200, mine.text
    assert [row["id"] for row in mine.json()] == [str(older.id)]
    assert mine.json()[0]["allowed_actions"] == ["accept", "decline"]
    assert pending.json() == mine.json()
    assert accepted.status_code == 200
    assert accepted.json() == []

    async with await h.client(member) as c:
        response = await c.get("/api/v1/me/invites")
    assert response.json() == []
    async with h.anonymous() as c:
        assert_problem(await c.get("/api/v1/me/invites"), 401, ErrorCode.UNAUTHENTICATED)

    async with h.db() as session:
        await session.execute(
            update(models.Invite).where(models.Invite.id == older.id).values(invited_by=None)
        )
    async with await h.client(first) as c:
        response = await c.get("/api/v1/me/invites")
    assert response.status_code == 200, response.text
    assert response.json()[0]["inviter"] is None
    assert response.json()[0]["invited_by"] is None


async def accept(h: Harness, actor: models.User, invite_id: UUID) -> httpx.Response:
    async with await h.client(actor) as c:
        return await c.post(f"/api/v1/invites/{invite_id}/accept")


async def live_members(h: Harness, *conditions: Any) -> list[models.ProjectMember]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.ProjectMember).where(
                    models.ProjectMember.left_at.is_(None), *conditions
                )
            )
        )


async def enrollment_rows(
    h: Harness, event: models.Event, user: models.User
) -> list[models.Enrollment]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.Enrollment).where(
                    models.Enrollment.event_id == event.id, models.Enrollment.user_id == user.id
                )
            )
        )


async def test_accept_core_invite(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    event = await make_hackathon(h)
    lead, other, invitee, second_lead = [await h.user() for _ in range(4)]
    team = await seed_team(h, event, lead, members=[other])
    second = await seed_team(h, event, second_lead, name="Second")
    enrollment = await enroll(h, event, invitee)
    async with h.db() as session:
        await session.execute(
            update(models.Enrollment)
            .where(models.Enrollment.id == enrollment.id)
            .values(willing_to_lead=True)
        )
    target = await seed_invite(h, team, invitee, lead)
    async with h.db() as session:
        await session.execute(
            update(models.Invite)
            .where(models.Invite.id == target.id)
            .values(area_id=areas["backend"].id)
        )
        session.add(
            models.JoinRequest(
                project_id=second.id,
                event_id=event.id,
                user_id=invitee.id,
                note="please",
                status="pending",
            )
        )
    rival = await seed_invite(h, second, invitee, second_lead)

    response = await accept(h, invitee, target.id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "accepted"
    assert datetime.fromisoformat(body["responded_at"]) == h.now
    assert body["allowed_actions"] == []
    assert body["closed_reason"] is None
    assert datetime.fromisoformat(body["expires_at"]) == target.expires_at

    [member] = await live_members(h, models.ProjectMember.user_id == invitee.id)
    assert member.project_id == team.id
    assert member.role == "member"
    assert member.state == "confirmed"
    assert member.is_team_row is True
    assert member.joined_via == "core_invite"
    assert member.area_id == areas["backend"].id
    assert member.confirmed_at == h.now
    assert member.confirm_by is None
    [row] = await enrollment_rows(h, event, invitee)
    assert row.status == "placed"
    assert row.status_at == h.now
    assert row.reserve_rank is None
    assert row.preferred_area_id == areas["backend"].id
    assert row.note == "hi"
    assert row.willing_to_lead is True
    async with h.db() as session:
        closed = await session.get_one(models.Invite, rival.id)
        request = await session.scalar(
            select(models.JoinRequest).where(models.JoinRequest.user_id == invitee.id)
        )
    assert closed.status == "closed"
    assert closed.closed_reason == "joined_team"
    assert closed.responded_at == h.now
    assert request is not None
    assert request.status == "closed"
    assert request.responded_at == h.now
    assert len(await notifications_for(h, lead, "invite_accepted")) == 1
    [closed_notice] = await notifications_for(h, second_lead, "invite_closed")
    assert cast(dict[str, Any], closed_notice.payload)["reason"]
    assert len(await h.pending_jobs("refresh_recommendations")) == 2
    assert await h.pending_jobs("rank_reserve") == []


async def test_accept_without_live_enrollment(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    lead, withdrawn, fresh = await h.user(), await h.user(), await h.user()
    team = await seed_team(h, event, lead)
    stored = await enroll(h, event, withdrawn, status="withdrawn")
    first = await seed_invite(h, team, withdrawn, lead)
    second = await seed_invite(h, team, fresh, lead)

    assert (await accept(h, withdrawn, first.id)).status_code == 200
    [row] = await enrollment_rows(h, event, withdrawn)
    assert row.id == stored.id
    assert row.status == "placed"
    assert row.status_at == h.now
    assert row.source == "self"

    assert (await accept(h, fresh, second.id)).status_code == 200
    [created] = await enrollment_rows(h, event, fresh)
    assert created.status == "placed"
    assert created.source == "invite"


async def test_accept_fill_from_reserve(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    lead, a, b, invitee = [await h.user() for _ in range(4)]
    team = await seed_team(h, event, lead, team_status="review", members=[a, b])
    enrollment = await enroll(h, event, invitee, status="reserve")
    async with h.db() as session:
        await session.execute(
            update(models.Enrollment)
            .where(models.Enrollment.id == enrollment.id)
            .values(reserve_rank=2)
        )
    fill = await seed_invite(h, team, invitee, lead, kind="fill")

    assert (await accept(h, invitee, fill.id)).status_code == 200
    [member] = await live_members(h, models.ProjectMember.user_id == invitee.id)
    assert member.joined_via == "fill_invite"
    [row] = await enrollment_rows(h, event, invitee)
    assert row.status == "placed"
    assert row.reserve_rank is None
    [job] = await h.pending_jobs("rank_reserve")
    assert job["args"]["event_id"] == str(event.id)
    assert len(await h.pending_jobs("refresh_recommendations")) == 1


async def test_accept_retry(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    lead, invitee = await h.user(), await h.user()
    team = await seed_team(h, event, lead)
    target = await seed_invite(h, team, invitee, lead)

    first = await accept(h, invitee, target.id)
    assert first.status_code == 200, first.text
    jobs = len(await h.pending_jobs())
    second = await accept(h, invitee, target.id)
    assert second.status_code == 200, second.text
    assert second.json() == first.json()
    assert len(await live_members(h, models.ProjectMember.user_id == invitee.id)) == 1
    assert len(await notifications_for(h, lead, "invite_accepted")) == 1
    assert len(await h.pending_jobs()) == jobs


async def test_accept_gates(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h, team_size_max=5)
    lead, invitee = await h.user(), await h.user()
    team = await seed_team(h, event, lead)

    expired = await seed_invite(h, team, invitee, lead, expires_in=timedelta(seconds=-1))
    assert_problem(await accept(h, invitee, expired.id), 409, ErrorCode.INVALID_STATE)
    async with h.db() as session:
        assert (await session.get_one(models.Invite, expired.id)).status == "pending"
        await session.execute(delete(models.Invite).where(models.Invite.id == expired.id))

    target = await seed_invite(h, team, invitee, lead)
    await set_formation(h, event, phase="forming")
    assert_problem(await accept(h, invitee, target.id), 409, ErrorCode.FORMATION_IN_PROGRESS)
    await set_formation(h, event, phase="open")

    async with h.db() as session:
        await session.execute(
            update(models.Project)
            .where(models.Project.id == team.id)
            .values(team_status="disbanded")
        )
    assert_problem(await accept(h, invitee, target.id), 409, ErrorCode.INVALID_STATE)
    async with h.db() as session:
        await session.execute(
            update(models.Project).where(models.Project.id == team.id).values(team_status="forming")
        )

    full_lead = await h.user()
    full = await seed_team(
        h, event, full_lead, members=[await h.user() for _ in range(4)], name="Full"
    )
    full_invite = await seed_invite(h, full, invitee, full_lead)
    assert_problem(await accept(h, invitee, full_invite.id), 409, ErrorCode.TEAM_FULL)

    taken = await h.user()
    await seed_team(h, event, await h.user(), members=[taken], name="Taken")
    taken_invite = await seed_invite(h, team, taken, lead)
    assert_problem(await accept(h, taken, taken_invite.id), 409, ErrorCode.ALREADY_IN_TEAM)

    assert_problem(await accept(h, await h.user(), target.id), 403, ErrorCode.FORBIDDEN)
    assert_problem(await accept(h, lead, target.id), 403, ErrorCode.FORBIDDEN)
    assert_problem(await accept(h, invitee, uuid4()), 404, ErrorCode.NOT_FOUND)
    async with h.anonymous() as c:
        response = await c.post(f"/api/v1/invites/{target.id}/accept")
    assert_problem(response, 401, ErrorCode.UNAUTHENTICATED)


async def test_accept_terminal_and_dnd(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    lead, declined_user, withdrawn_user, dnd_user = [await h.user() for _ in range(4)]
    team = await seed_team(h, event, lead)
    declined = await seed_invite(h, team, declined_user, lead, status="declined")
    withdrawn = await seed_invite(h, team, withdrawn_user, lead, status="withdrawn")
    assert_problem(await accept(h, declined_user, declined.id), 409, ErrorCode.INVALID_STATE)
    assert_problem(await accept(h, withdrawn_user, withdrawn.id), 409, ErrorCode.INVALID_STATE)

    target = await seed_invite(h, team, dnd_user, lead)
    async with h.db() as session:
        await session.execute(
            update(models.User).where(models.User.id == dnd_user.id).values(dnd_indefinite=True)
        )
    response = await accept(h, dnd_user, target.id)
    assert response.status_code == 200, response.text


async def test_accept_standalone_invite(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    lead, member = await h.user(), await h.user()
    project = await seed_standalone(h, lead)
    target = await seed_invite(h, project, member, lead, kind="project")
    response = await accept(h, member, target.id)
    assert response.status_code == 200, response.text
    [row] = await live_members(h, models.ProjectMember.user_id == member.id)
    assert row.is_team_row is False
    assert row.event_id is None
    assert row.joined_via == "invite"
    assert row.state == "confirmed"
    assert await count(h, models.Enrollment, models.Enrollment.user_id == member.id) == 0
    assert await h.pending_jobs() == []
    assert len(await notifications_for(h, lead, "invite_accepted")) == 1

    full_lead = await h.user()
    full = await seed_standalone(h, full_lead, members=[await h.user() for _ in range(19)])
    outsider = await h.user()
    full_invite = await seed_invite(h, full, outsider, full_lead, kind="project")
    assert_problem(await accept(h, outsider, full_invite.id), 409, ErrorCode.PROJECT_FULL)


async def test_accept_prevents_double_booking(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    lead, other_lead, member = await h.user(), await h.user(), await h.user()
    team = await seed_team(h, event, lead)
    other = await seed_team(h, event, other_lead, name="Other")
    target = await seed_invite(h, team, member, lead)
    rival = await seed_invite(h, other, member, other_lead)

    assert (await accept(h, member, target.id)).status_code == 200
    async with h.db() as session:
        assert (await session.get_one(models.Invite, rival.id)).status == "closed"
    assert_problem(await accept(h, member, rival.id), 409, ErrorCode.INVALID_STATE)
    rows = await live_members(
        h, models.ProjectMember.user_id == member.id, models.ProjectMember.event_id == event.id
    )
    assert len(rows) == 1


async def test_accept_concurrency(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    lead, invitee = await h.user(), await h.user()
    team = await seed_team(h, event, lead)
    target = await seed_invite(h, team, invitee, lead)
    first, second = await asyncio.gather(
        accept(h, invitee, target.id), accept(h, invitee, target.id)
    )
    assert (first.status_code, second.status_code) == (200, 200)
    assert len(await live_members(h, models.ProjectMember.user_id == invitee.id)) == 1
    assert len(await notifications_for(h, lead, "invite_accepted")) == 1

    small = await make_hackathon(h, team_size_max=4)
    small_lead, a, b, x, y = [await h.user() for _ in range(5)]
    crowded = await seed_team(h, small, small_lead, members=[a, b])
    invite_x = await seed_invite(h, crowded, x, small_lead)
    invite_y = await seed_invite(h, crowded, y, small_lead)
    results = await asyncio.gather(accept(h, x, invite_x.id), accept(h, y, invite_y.id))
    assert sorted(response.status_code for response in results) == [200, 409]
    [failed] = [response for response in results if response.status_code == 409]
    assert_problem(failed, 409, ErrorCode.TEAM_FULL)
    assert len(await live_members(h, models.ProjectMember.project_id == crowded.id)) == 4


async def decline(h: Harness, actor: models.User, invite_id: UUID) -> httpx.Response:
    async with await h.client(actor) as c:
        return await c.post(f"/api/v1/invites/{invite_id}/decline")


async def withdraw(h: Harness, actor: models.User, invite_id: UUID) -> httpx.Response:
    async with await h.client(actor) as c:
        return await c.delete(f"/api/v1/invites/{invite_id}")


async def invite_row(h: Harness, invite_id: UUID) -> models.Invite:
    async with h.db() as session:
        return await session.get_one(models.Invite, invite_id)


async def project_row(h: Harness, project_id: UUID) -> models.Project:
    async with h.db() as session:
        return await session.get_one(models.Project, project_id)


async def test_decline_invite(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    lead, invitee = await h.user(), await h.user()
    team = await seed_team(h, event, lead)
    enrollment = await enroll(h, event, invitee)
    target = await seed_invite(h, team, invitee, lead)
    jobs = len(await h.pending_jobs())
    h.advance(timedelta(minutes=5))

    response = await decline(h, invitee, target.id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "declined"
    assert datetime.fromisoformat(body["responded_at"]) == h.now
    assert body["allowed_actions"] == []
    assert await live_members(h, models.ProjectMember.user_id == invitee.id) == []
    [row] = await enrollment_rows(h, event, invitee)
    assert (row.status, row.status_at) == (enrollment.status, enrollment.status_at)
    assert (await project_row(h, team.id)).last_lead_action_at == team.last_lead_action_at
    assert len(await notifications_for(h, lead, "invite_declined")) == 1
    assert len(await h.pending_jobs()) == jobs

    again = await decline(h, invitee, target.id)
    assert again.status_code == 200, again.text
    assert again.json() == body
    assert len(await notifications_for(h, lead, "invite_declined")) == 1


async def test_decline_gates(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    lead, member, invitee = await h.user(), await h.user(), await h.user()
    team = await seed_team(h, event, lead, members=[member])

    accepted = await seed_invite(h, team, await h.user(), lead, status="accepted")
    async with h.db() as session:
        accepted_user = await session.get_one(models.User, accepted.user_id)
    assert_problem(await decline(h, accepted_user, accepted.id), 409, ErrorCode.INVALID_STATE)

    expired = await seed_invite(h, team, invitee, lead, expires_in=timedelta(seconds=-1))
    assert_problem(await decline(h, invitee, expired.id), 409, ErrorCode.INVALID_STATE)
    assert (await invite_row(h, expired.id)).status == "pending"
    async with h.db() as session:
        await session.execute(delete(models.Invite).where(models.Invite.id == expired.id))

    target = await seed_invite(h, team, invitee, lead)
    assert_problem(await decline(h, member, target.id), 403, ErrorCode.FORBIDDEN)
    await set_formation(h, event, phase="forming")
    assert_problem(await decline(h, invitee, target.id), 409, ErrorCode.FORMATION_IN_PROGRESS)
    await set_formation(h, event, phase="open")
    async with h.db() as session:
        await session.execute(
            update(models.Project)
            .where(models.Project.id == team.id)
            .values(team_status="disbanded")
        )
    assert_problem(await decline(h, invitee, target.id), 409, ErrorCode.INVALID_STATE)

    standalone_lead, standalone_invitee = await h.user(), await h.user()
    abandoned = await seed_standalone(h, standalone_lead, status="abandoned")
    closed = await seed_invite(h, abandoned, standalone_invitee, standalone_lead, kind="project")
    assert_problem(await decline(h, standalone_invitee, closed.id), 409, ErrorCode.INVALID_STATE)
    assert_problem(await decline(h, invitee, uuid4()), 404, ErrorCode.NOT_FOUND)
    async with h.anonymous() as c:
        response = await c.post(f"/api/v1/invites/{target.id}/decline")
    assert_problem(response, 401, ErrorCode.UNAUTHENTICATED)


async def test_withdraw_invite(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    lead, invitee = await h.user(), await h.user()
    team = await seed_team(h, event, lead)
    enrollment = await enroll(h, event, invitee)
    target = await seed_invite(h, team, invitee, lead)
    jobs = len(await h.pending_jobs())

    response = await withdraw(h, lead, target.id)
    assert response.status_code == 204, response.text
    assert response.content == b""
    row = await invite_row(h, target.id)
    assert row.status == "withdrawn"
    assert row.responded_at == h.now
    assert (await project_row(h, team.id)).last_lead_action_at == h.now
    [notice] = await notifications_for(h, invitee, "invite_closed")
    assert cast(dict[str, Any], notice.payload)["reason"]
    assert await live_members(h, models.ProjectMember.user_id == invitee.id) == []
    [stored] = await enrollment_rows(h, event, invitee)
    assert (stored.status, stored.status_at) == (enrollment.status, enrollment.status_at)
    assert len(await h.pending_jobs()) == jobs

    again = await withdraw(h, lead, target.id)
    assert again.status_code == 204, again.text
    assert len(await notifications_for(h, invitee, "invite_closed")) == 1


async def test_withdraw_gates(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    lead, member, invitee = await h.user(), await h.user(), await h.user()
    team = await seed_team(h, event, lead, members=[member])
    target = await seed_invite(h, team, invitee, lead)

    assert_problem(await withdraw(h, member, target.id), 403, ErrorCode.FORBIDDEN)
    assert_problem(await withdraw(h, await h.admin(), target.id), 403, ErrorCode.FORBIDDEN)
    accepted = await seed_invite(h, team, await h.user(), lead, status="accepted")
    assert_problem(await withdraw(h, lead, accepted.id), 409, ErrorCode.INVALID_STATE)
    await set_formation(h, event, phase="forming")
    assert_problem(await withdraw(h, lead, target.id), 409, ErrorCode.FORMATION_IN_PROGRESS)
    await set_formation(h, event, phase="open")
    assert_problem(await withdraw(h, lead, uuid4()), 404, ErrorCode.NOT_FOUND)

    assert (await withdraw(h, lead, target.id)).status_code == 204
    response = await invite(h, lead, team.id, invitee)
    assert response.status_code == 201, response.text


async def test_withdraw_standalone_invite(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    lead, invitee = await h.user(), await h.user()
    project = await seed_standalone(h, lead)
    target = await seed_invite(h, project, invitee, lead, kind="project")
    response = await withdraw(h, lead, target.id)
    assert response.status_code == 204, response.text
    assert len(await notifications_for(h, invitee, "invite_closed")) == 1
