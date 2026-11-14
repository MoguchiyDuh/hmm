import itertools
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
from harness import Harness
from hmm_contracts.common import (
    AreaImportance,
    EventKind,
    EventOrigin,
    EventStatus,
    LeadOfferStatus,
    ProjectOrigin,
    ProjectStatus,
    ProjectVisibility,
    TeamStatus,
)
from hmm_core import models
from hmm_core.catalog import service as catalog
from hmm_core.formation import service as formation
from sqlalchemy import func, select

_titles = itertools.count(1)


async def make_hackathon(
    h: Harness,
    *,
    team_size_min: int | None = None,
    team_size_max: int | None = None,
    starts_in: timedelta = timedelta(days=30),
    registration_deadline: datetime | None = None,
) -> models.Event:
    async with h.db() as session:
        return await catalog.create_event(
            session,
            catalog.EventInput(
                kind=EventKind.HACKATHON,
                title=f"Ban Hack {next(_titles)}",
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


async def enroll(
    h: Harness,
    event: models.Event,
    user: models.User,
    *,
    status: str = "placed",
    willing: bool = False,
) -> models.Enrollment:
    async with h.db() as session:
        existing = await formation.live_enrollment_row(session, event.id, user.id)
        if existing is not None:
            return existing
        enrollment = models.Enrollment(
            event_id=event.id,
            user_id=user.id,
            status=status,
            source="self",
            willing_to_lead=willing,
            status_at=h.now,
        )
        session.add(enrollment)
        await session.flush()
        await session.refresh(enrollment)
        return enrollment


async def add_member(
    h: Harness,
    event: models.Event,
    project: models.Project,
    user: models.User,
    *,
    role: str = "member",
    state: str = "confirmed",
    joined_via: str = "admin",
    willing: bool = False,
    fit: float | None = None,
) -> models.ProjectMember:
    await enroll(h, event, user, willing=willing)
    async with h.db() as session:
        member = models.ProjectMember(
            project_id=project.id,
            event_id=event.id,
            user_id=user.id,
            role=role,
            area_id=None,
            state=state,
            trust="self",
            joined_via=joined_via,
            proposal_reason=None,
            confirm_by=h.now + timedelta(days=1) if state == "proposed" else None,
            confirm_kind="placement" if state == "proposed" else None,
            is_team_row=True,
            joined_at=h.now,
            confirmed_at=h.now if state == "confirmed" else None,
        )
        session.add(member)
        if fit is not None:
            session.add(
                models.LeadFit(
                    event_id=event.id,
                    user_id=user.id,
                    fit=Decimal(str(fit)),
                    reason="fit",
                    algo_version="fake-1",
                    computed_at=h.now,
                )
            )
        await session.flush()
        await session.refresh(member)
        return member


async def make_team(
    h: Harness,
    event: models.Event,
    lead: models.User | None,
    members: tuple[models.User, ...] = (),
    *,
    team_status: str = TeamStatus.FORMING.value,
    lead_fit: float | None = None,
    fits: tuple[float | None, ...] = (),
) -> models.Project:
    async with h.db() as session:
        project = models.Project(
            event_id=event.id,
            name="Core",
            description_md="",
            goal="",
            links=[],
            visibility=ProjectVisibility.PUBLIC.value,
            recruiting=None,
            lead_user_id=lead.id if lead is not None else None,
            status=ProjectStatus.IDEA.value,
            team_status=team_status,
            origin=ProjectOrigin.CLAIM.value,
        )
        session.add(project)
        await session.flush()
        await session.refresh(project)
    if lead is not None:
        await add_member(h, event, project, lead, role="lead", fit=lead_fit)
    for index, user in enumerate(members):
        await add_member(h, event, project, user, fit=fits[index] if index < len(fits) else None)
    return project


async def ban(h: Harness, admin: models.User, user: models.User) -> httpx.Response:
    async with await h.client(admin) as c:
        return await c.post(f"/api/v1/admin/users/{user.id}/ban", json={"reason": "spam"})


async def member_row(
    h: Harness, project: models.Project, user: models.User
) -> models.ProjectMember:
    async with h.db() as session:
        row = await session.scalar(
            select(models.ProjectMember).where(
                models.ProjectMember.project_id == project.id,
                models.ProjectMember.user_id == user.id,
            )
        )
        assert row is not None
        return row


async def stored_project(h: Harness, project_id: UUID) -> models.Project:
    async with h.db() as session:
        return await session.get_one(models.Project, project_id)


async def rows(h: Harness, model: Any, *conditions: Any) -> list[Any]:
    async with h.db() as session:
        return list(await session.scalars(select(model).where(*conditions)))


async def count(h: Harness, model: Any, *conditions: Any) -> int:
    async with h.db() as session:
        return await session.scalar(select(func.count()).select_from(model).where(*conditions)) or 0


async def snapshot(h: Harness) -> tuple[int, int, int, int, int]:
    return (
        await count(h, models.ProjectMember, models.ProjectMember.left_at.is_not(None)),
        await count(h, models.AdminAlert, models.AdminAlert.id.is_not(None)),
        await count(h, models.Notification, models.Notification.id.is_not(None)),
        await count(h, models.AuditLog, models.AuditLog.id.is_not(None)),
        await count(h, models.LeadOffer, models.LeadOffer.id.is_not(None)),
    )


def notices_of(notifications: list[models.Notification], kind: str) -> list[UUID]:
    return sorted(n.user_id for n in notifications if n.type == kind)


async def test_ban_removes_member_from_review_team(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    admin = await h.admin()
    event = await make_hackathon(h)
    lead, banned, other = await h.user(), await h.user(), await h.user()
    team = await make_team(h, event, lead, (banned, other), team_status=TeamStatus.REVIEW.value)

    response = await ban(h, admin, banned)
    assert response.status_code == 200, response.text
    row = await member_row(h, team, banned)
    assert (row.left_at, row.left_reason) == (h.now, "banned")
    [enrollment] = await rows(
        h,
        models.Enrollment,
        models.Enrollment.event_id == event.id,
        models.Enrollment.user_id == banned.id,
    )
    assert enrollment.status == "withdrawn"
    notices = await rows(h, models.Notification, models.Notification.type == "team_member_left")
    assert sorted(n.user_id for n in notices) == sorted([lead.id, other.id])
    for notice in notices:
        assert isinstance(notice.payload, dict)
        assert notice.payload["reason"]
        assert notice.payload["member_count"] == 2
    assert await h.pending_jobs("refresh_recommendations") == []
    assert await h.pending_jobs("rank_reserve") == []
    assert [a.action for a in await rows(h, models.AuditLog)] == ["user_ban"]
    assert (await stored_project(h, team.id)).lead_user_id == lead.id


async def test_ban_raises_team_below_min(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    admin = await h.admin()
    small_event = await make_hackathon(h, team_size_min=3)
    large_event = await make_hackathon(h, team_size_min=3)
    banned = await h.user()
    small = await make_team(
        h,
        small_event,
        await h.user(),
        (await h.user(), banned),
        team_status=TeamStatus.LOCKED.value,
    )
    large = await make_team(
        h,
        large_event,
        await h.user(),
        (await h.user(), await h.user(), banned),
        team_status=TeamStatus.LOCKED.value,
    )

    assert (await ban(h, admin, banned)).status_code == 200
    alerts = await rows(h, models.AdminAlert, models.AdminAlert.type == "team_below_min")
    assert [(a.status, a.severity, a.subject_id, a.dedupe_key) for a in alerts] == [
        ("open", "warning", small.id, f"team_below_min:{small.id}")
    ]
    assert large.id not in {a.subject_id for a in alerts}


async def test_ban_review_team_lead(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    admin = await h.admin()
    event = await make_hackathon(h)
    lead, strong, weak = await h.user(), await h.user(), await h.user()
    team = await make_team(
        h, event, lead, (strong, weak), team_status=TeamStatus.REVIEW.value, fits=(0.9, 0.4)
    )
    async with h.db() as session:
        transfer = models.LeadOffer(
            project_id=team.id,
            event_id=event.id,
            user_id=weak.id,
            source="transfer",
            offered_by=lead.id,
            tier=None,
            reason="r",
            status=LeadOfferStatus.PENDING.value,
            expires_at=h.now + timedelta(hours=12),
        )
        session.add(transfer)
        await session.flush()
        transfer_id = transfer.id

    assert (await ban(h, admin, lead)).status_code == 200
    assert (await stored_project(h, team.id)).lead_user_id is None
    assert (await member_row(h, team, lead)).left_reason == "banned"
    [cancelled] = await rows(h, models.LeadOffer, models.LeadOffer.id == transfer_id)
    assert cancelled.status == "cancelled"
    [succession] = await rows(
        h,
        models.LeadOffer,
        models.LeadOffer.project_id == team.id,
        models.LeadOffer.status == LeadOfferStatus.PENDING.value,
    )
    assert (succession.user_id, succession.source) == (strong.id, "succession")
    steps = await rows(h, models.AuditLog, models.AuditLog.action == "lead_step_down")
    assert [(a.actor_kind, a.actor_id) for a in steps] == [("admin", admin.id)]
    notices = await rows(h, models.Notification)
    assert notices_of(notices, "team_member_left") == sorted([strong.id, weak.id])


async def test_ban_empties_forming_core(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    admin = await h.admin()
    event = await make_hackathon(h)
    lead = await h.user()
    team = await make_team(h, event, lead)

    assert (await ban(h, admin, lead)).status_code == 200
    project = await stored_project(h, team.id)
    assert (project.team_status, project.status, project.lead_user_id) == (
        "disbanded",
        "disbanded",
        None,
    )
    [disband] = await rows(h, models.AuditLog, models.AuditLog.action == "project_disband")
    assert disband.before == {"team_status": "forming"}
    assert isinstance(disband.after, dict)
    assert disband.after["team_status"] == "disbanded"
    assert await count(h, models.LeadOffer, models.LeadOffer.project_id == team.id) == 0
    assert await count(h, models.Notification, models.Notification.type == "team_member_left") == 0


async def test_ban_forming_core_lead_passes_role(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    admin = await h.admin()
    event = await make_hackathon(h)
    lead, member = await h.user(), await h.user()
    team = await make_team(h, event, lead, (member,))

    assert (await ban(h, admin, lead)).status_code == 200
    [succession] = await rows(
        h,
        models.LeadOffer,
        models.LeadOffer.project_id == team.id,
        models.LeadOffer.status == LeadOfferStatus.PENDING.value,
    )
    assert succession.user_id == member.id
    assert (await stored_project(h, team.id)).team_status == "forming"


async def test_ban_keeps_active_team_rows(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    admin = await h.admin()
    event = await make_hackathon(h)
    lead, banned = await h.user(), await h.user()
    team = await make_team(h, event, lead, (banned,), team_status=TeamStatus.ACTIVE.value)

    assert (await ban(h, admin, banned)).status_code == 200
    row = await member_row(h, team, banned)
    assert (row.left_at, row.role) == (None, "member")
    alerts = await rows(
        h, models.AdminAlert, models.AdminAlert.type == "banned_member_in_active_team"
    )
    assert [(a.status, a.severity, a.subject_id) for a in alerts] == [("open", "warning", team.id)]
    notices = await rows(h, models.Notification)
    assert notices_of(notices, "admin_alert") == [admin.id]
    assert {n.user_id for n in notices} == {admin.id}

    other_event = await make_hackathon(h)
    active_lead = await h.user()
    led = await make_team(
        h, other_event, active_lead, (await h.user(),), team_status=TeamStatus.ACTIVE.value
    )
    assert (await ban(h, admin, active_lead)).status_code == 200
    assert (await member_row(h, led, active_lead)).role == "lead"
    assert (await stored_project(h, led.id)).lead_user_id == active_lead.id


async def test_ban_leaves_finished_team_untouched(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    admin = await h.admin()
    event = await make_hackathon(h)
    lead, banned = await h.user(), await h.user()
    team = await make_team(h, event, lead, (banned,), team_status=TeamStatus.FINISHED.value)

    assert (await ban(h, admin, banned)).status_code == 200
    assert (await member_row(h, team, banned)).left_at is None
    assert await count(h, models.AdminAlert, models.AdminAlert.id.is_not(None)) == 0
    assert await count(h, models.Notification, models.Notification.id.is_not(None)) == 0
    assert await count(h, models.AuditLog, models.AuditLog.entity_id == str(team.id)) == 0


async def test_ban_mixed_teams(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    admin = await h.admin()
    banned = await h.user()
    teams: dict[str, models.Project] = {}
    for status in ("forming", "review", "locked", "active", "finished"):
        event = await make_hackathon(h)
        teams[status] = await make_team(
            h, event, await h.user(), (banned, await h.user()), team_status=status
        )

    async with h.db() as session:
        user = await session.get_one(models.User, banned.id)
        removed = await formation.remove_user_from_teams(session, user.id, h.now, actor=admin.id)
    assert removed == sorted(teams[status].id for status in ("forming", "review", "locked"))
    closed = await rows(
        h,
        models.ProjectMember,
        models.ProjectMember.user_id == banned.id,
        models.ProjectMember.left_at.is_not(None),
    )
    assert sorted(row.project_id for row in closed) == removed
    assert (
        await count(h, models.AdminAlert, models.AdminAlert.type == "banned_member_in_active_team")
        == 1
    )

    other = await h.user()
    other_teams: list[models.Project] = []
    for status in ("forming", "review", "locked", "active", "finished"):
        event = await make_hackathon(h)
        other_teams.append(
            await make_team(h, event, await h.user(), (other, await h.user()), team_status=status)
        )
    assert (await ban(h, admin, other)).status_code == 200
    assert (
        await count(
            h,
            models.ProjectMember,
            models.ProjectMember.user_id == other.id,
            models.ProjectMember.left_at.is_not(None),
        )
        == 3
    )
    assert (await stored_project(h, other_teams[0].id)).team_status == "forming"
    async with h.db() as session:
        assert (await session.get_one(models.User, other.id)).banned_at == h.now


async def test_repeat_ban_changes_nothing(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    admin = await h.admin()
    event = await make_hackathon(h)
    active_event = await make_hackathon(h)
    banned = await h.user()
    await make_team(h, event, await h.user(), (banned, await h.user()), team_status="review")
    await make_team(h, active_event, await h.user(), (banned, await h.user()), team_status="active")
    assert (await ban(h, admin, banned)).status_code == 200
    before = await snapshot(h)
    h.advance(timedelta(minutes=5))
    assert (await ban(h, admin, banned)).status_code == 200
    assert await snapshot(h) == before


async def test_remove_user_without_teams(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    user = await h.user()
    before = await snapshot(h)
    async with h.db() as session:
        assert await formation.remove_user_from_teams(session, user.id, h.now, actor=admin.id) == []
    assert await snapshot(h) == before


async def pending_offer(
    h: Harness,
    project: models.Project,
    user: models.User,
    *,
    source: str,
    offered_by: models.User | None = None,
) -> UUID:
    async with h.db() as session:
        offer = models.LeadOffer(
            project_id=project.id,
            event_id=project.event_id,
            user_id=user.id,
            source=source,
            offered_by=offered_by.id if offered_by is not None else None,
            tier=None,
            reason="r",
            status=LeadOfferStatus.PENDING.value,
            expires_at=h.now + timedelta(hours=12),
        )
        session.add(offer)
        await session.flush()
        return offer.id


async def test_ban_closes_succession_offer_and_advances(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    admin = await h.admin()
    event = await make_hackathon(h)
    banned, other = await h.user(), await h.user()
    team = await make_team(
        h, event, None, (banned, other), team_status=TeamStatus.REVIEW.value, fits=(0.9, 0.5)
    )
    offer_id = await pending_offer(h, team, banned, source="succession")

    assert (await ban(h, admin, banned)).status_code == 200
    [closed] = await rows(h, models.LeadOffer, models.LeadOffer.id == offer_id)
    assert (closed.status, closed.responded_at) == ("closed", h.now)
    [following] = await rows(
        h,
        models.LeadOffer,
        models.LeadOffer.project_id == team.id,
        models.LeadOffer.status == LeadOfferStatus.PENDING.value,
    )
    assert (following.user_id, following.source) == (other.id, "succession")
    project = await stored_project(h, team.id)
    assert project.lead_user_id is None
    assert [str(value) for value in project.lead_succession_excluded] == [str(banned.id)]


async def test_ban_closes_transfer_offer_and_keeps_lead(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    admin = await h.admin()
    event = await make_hackathon(h)
    lead, banned, other = await h.user(), await h.user(), await h.user()
    team = await make_team(h, event, lead, (banned, other), team_status=TeamStatus.LOCKED.value)
    offer_id = await pending_offer(h, team, banned, source="transfer", offered_by=lead)

    assert (await ban(h, admin, banned)).status_code == 200
    [closed] = await rows(h, models.LeadOffer, models.LeadOffer.id == offer_id)
    assert (closed.status, closed.responded_at) == ("closed", h.now)
    assert (
        await count(
            h,
            models.LeadOffer,
            models.LeadOffer.project_id == team.id,
            models.LeadOffer.status == LeadOfferStatus.PENDING.value,
        )
        == 0
    )
    project = await stored_project(h, team.id)
    assert project.lead_user_id == lead.id
    assert project.lead_succession_excluded == []
    assert (await member_row(h, team, lead)).role == "lead"
