import asyncio
import itertools
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from harness import Harness
from hmm_contracts.common import (
    ActorKind,
    AreaImportance,
    ErrorCode,
    EventKind,
    EventOrigin,
    EventStatus,
    LeadOfferSource,
    LeadOfferStatus,
    LeftReason,
    ProjectOrigin,
    ProjectStatus,
    ProjectVisibility,
    TeamStatus,
)
from hmm_core import models
from hmm_core.catalog import service as catalog
from hmm_core.formation import service as formation
from hmm_core.platform.errors import ProblemError
from sqlalchemy import func, select, update
from test_api_skeleton import assert_problem

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
                title=f"Offer Hack {next(_titles)}",
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
    event: models.Event | None,
    *,
    lead: models.User | None = None,
    team_status: str | None = TeamStatus.FORMING.value,
    **fields: Any,
) -> models.Project:
    async with h.db() as session:
        project = models.Project(
            event_id=event.id if event is not None else None,
            name="Core",
            description_md="",
            goal="",
            links=[],
            visibility=ProjectVisibility.PUBLIC.value,
            recruiting=None,
            lead_user_id=lead.id if lead is not None else None,
            status=fields.pop("status", ProjectStatus.IDEA.value),
            team_status=team_status,
            origin=ProjectOrigin.CLAIM.value,
            **fields,
        )
        session.add(project)
        await session.flush()
        await session.refresh(project)
        return project


async def order_of(h: Harness, project: models.Project) -> list[formation.LeadOrderEntry]:
    async with h.db() as session:
        row = await session.get_one(models.Project, project.id)
        return await formation.lead_order(session, row, h.now)


async def expiry_of(h: Harness, project: models.Project, event: models.Event | None) -> datetime:
    async with h.db() as session:
        row = await session.get_one(models.Project, project.id)
        loaded = await session.get_one(models.Event, event.id) if event is not None else None
        return await formation.lead_offer_expiry(session, row, loaded, h.now)


async def succession(
    h: Harness, project: models.Project, event: models.Event, exclude: set[UUID]
) -> tuple[UUID | None, list[str], UUID | None]:
    async with h.db() as session:
        row = await session.get_one(models.Project, project.id)
        loaded = await session.get_one(models.Event, event.id)
        offer = await formation.start_succession(
            session, row, loaded, h.now, exclude=exclude, reason="The lead stepped down"
        )
        await session.flush()
        return (
            offer.user_id if offer is not None else None,
            [str(value) for value in row.lead_succession_excluded],
            row.lead_user_id,
        )


async def force(h: Harness, project: models.Project, event: models.Event | None) -> UUID | None:
    async with h.db() as session:
        row = await session.get_one(models.Project, project.id)
        loaded = await session.get_one(models.Event, event.id) if event is not None else None
        member = await formation.force_lead(session, row, loaded, h.now, reason="Presence check")
        return member.user_id if member is not None else None


async def count(h: Harness, model: Any, *conditions: Any) -> int:
    async with h.db() as session:
        return await session.scalar(select(func.count()).select_from(model).where(*conditions)) or 0


async def test_lead_order_tiers_and_fits(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    lead = await h.user()
    team = await make_team(h, event, lead=lead)
    await add_member(h, event, team, lead, role="lead", fit=0.9)
    strong = await h.user()
    weak = await h.user()
    plain = await h.user()
    no_fit = await h.user()
    banned = await h.user(banned_at=h.now)
    await add_member(h, event, team, strong, willing=True, fit=0.8)
    await add_member(h, event, team, weak, willing=True, fit=0.4)
    await add_member(h, event, team, plain, fit=0.7)
    await add_member(h, event, team, no_fit)
    await add_member(h, event, team, banned, willing=True, fit=0.95)

    entries = await order_of(h, team)
    assert [entry.user.id for entry in entries] == [
        lead.id,
        strong.id,
        weak.id,
        plain.id,
        no_fit.id,
    ]
    assert [entry.tier for entry in entries] == [1, 2, 3, 4, 4]
    assert entries[-1].fit == 0.0

    async with h.db() as session:
        await session.execute(
            update(models.ProjectMember)
            .where(models.ProjectMember.user_id == plain.id)
            .values(
                state="proposed",
                confirmed_at=None,
                confirm_kind="placement",
                confirm_by=h.now + timedelta(days=1),
            )
        )
    assert [entry.user.id for entry in await order_of(h, team)] == [
        lead.id,
        strong.id,
        weak.id,
        plain.id,
        no_fit.id,
    ]


async def test_lead_order_tie_breaks(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    team = await make_team(h, event)
    first = await h.user()
    second = await h.user()
    await add_member(h, event, team, first, willing=True, fit=0.8)
    await add_member(h, event, team, second, willing=True, fit=0.8)
    async with h.db() as session:
        await session.execute(
            update(models.Enrollment)
            .where(models.Enrollment.event_id == event.id, models.Enrollment.user_id == first.id)
            .values(created_at=h.now)
        )
        await session.execute(
            update(models.Enrollment)
            .where(models.Enrollment.event_id == event.id, models.Enrollment.user_id == second.id)
            .values(created_at=h.now - timedelta(hours=1))
        )
    assert [entry.user.id for entry in await order_of(h, team)] == [second.id, first.id]

    async with h.db() as session:
        await session.execute(
            update(models.Enrollment)
            .where(models.Enrollment.event_id == event.id)
            .values(created_at=h.now)
        )
    expected = sorted([first.id, second.id], key=str)
    assert [entry.user.id for entry in await order_of(h, team)] == expected


async def test_lead_offer_expiry_caps(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    forming = await make_team(h, event)
    assert await expiry_of(h, forming, event) == h.now + timedelta(hours=12)
    async with h.db() as session:
        await session.execute(
            update(models.EventFormation)
            .where(models.EventFormation.event_id == event.id)
            .values(formation_at=h.now + timedelta(hours=3))
        )
    assert await expiry_of(h, forming, event) == h.now + timedelta(hours=3)

    review = await make_team(
        h, event, team_status=TeamStatus.REVIEW.value, review_ends_at=h.now + timedelta(hours=2)
    )
    assert await expiry_of(h, review, event) == h.now + timedelta(hours=2)

    soon = await make_hackathon(h, starts_in=timedelta(hours=1))
    locked = await make_team(h, soon, team_status=TeamStatus.LOCKED.value)
    assert await expiry_of(h, locked, soon) == h.now + timedelta(hours=1)

    running = await make_hackathon(h, starts_in=timedelta(days=3))
    active = await make_team(h, running, team_status=TeamStatus.ACTIVE.value)
    assert await expiry_of(h, active, running) == h.now + timedelta(hours=12)

    standalone = await make_team(h, None, team_status=None)
    assert await expiry_of(h, standalone, None) == h.now + timedelta(days=7)

    finished = await make_team(h, event, team_status=TeamStatus.FINISHED.value)
    assert await expiry_of(h, finished, event) == h.now + timedelta(days=7)

    disbanded = await make_team(
        h, event, team_status=TeamStatus.DISBANDED.value, status=ProjectStatus.DISBANDED.value
    )
    with pytest.raises(ProblemError) as error:
        await expiry_of(h, disbanded, event)
    assert error.value.code == ErrorCode.INVALID_STATE


async def test_create_lead_offer(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    lead = await h.user()
    first = await h.user()
    second = await h.user()
    outsider = await h.user()
    team = await make_team(h, event, lead=lead)
    await add_member(h, event, team, lead, role="lead")
    await add_member(h, event, team, first)
    await add_member(h, event, team, second)

    async with h.db() as session:
        row = await session.get_one(models.Project, team.id)
        loaded = await session.get_one(models.Event, event.id)
        offer = await formation.create_lead_offer(
            session,
            row,
            loaded,
            first.id,
            h.now,
            source=LeadOfferSource.TRANSFER,
            reason="Handing over",
            offered_by=lead.id,
            tier=2,
        )
        first_offer = offer.id
    async with h.db() as session:
        stored = await session.get_one(models.LeadOffer, first_offer)
        assert stored.status == LeadOfferStatus.PENDING.value
        assert stored.source == LeadOfferSource.TRANSFER.value
        assert stored.tier == 2
        assert stored.offered_by == lead.id
        assert stored.event_id == event.id
        assert stored.expires_at == h.now + timedelta(hours=12)
        notifications = list(
            await session.scalars(
                select(models.Notification).where(
                    models.Notification.user_id == first.id,
                    models.Notification.type == "lead_offer",
                )
            )
        )
    assert len(notifications) == 1
    payload = notifications[0].payload
    assert isinstance(payload, dict)
    assert payload["source"] == "transfer"
    assert payload["team_name"] == "Core"
    assert payload["expires_at"] is not None
    assert notifications[0].dedupe_key == f"lead_offer:{first_offer}"
    assert await h.pending_jobs() == []

    async with h.db() as session:
        row = await session.get_one(models.Project, team.id)
        loaded = await session.get_one(models.Event, event.id)
        await formation.create_lead_offer(
            session,
            row,
            loaded,
            second.id,
            h.now,
            source=LeadOfferSource.SUCCESSION,
            reason="Next in line",
        )
    async with h.db() as session:
        closed = await session.get_one(models.LeadOffer, first_offer)
        assert closed.status == LeadOfferStatus.CANCELLED.value
        assert closed.responded_at == h.now
    async with h.db() as session:
        row = await session.get_one(models.Project, team.id)
        loaded = await session.get_one(models.Event, event.id)
        await formation.create_lead_offer(
            session,
            row,
            loaded,
            first.id,
            h.now,
            source=LeadOfferSource.SUCCESSION,
            reason="Again",
        )
    assert (
        await count(
            h,
            models.LeadOffer,
            models.LeadOffer.project_id == team.id,
            models.LeadOffer.status == LeadOfferStatus.CLOSED.value,
            models.LeadOffer.user_id == second.id,
            models.LeadOffer.responded_at == h.now,
        )
        == 1
    )
    assert (
        await count(
            h,
            models.LeadOffer,
            models.LeadOffer.project_id == team.id,
            models.LeadOffer.status == LeadOfferStatus.PENDING.value,
        )
        == 1
    )

    for user_id, code in ((outsider.id, ErrorCode.INVALID_STATE), (uuid4(), ErrorCode.NOT_FOUND)):
        async with h.db() as session:
            row = await session.get_one(models.Project, team.id)
            loaded = await session.get_one(models.Event, event.id)
            with pytest.raises(ProblemError) as error:
                await formation.create_lead_offer(
                    session,
                    row,
                    loaded,
                    user_id,
                    h.now,
                    source=LeadOfferSource.SUCCESSION,
                    reason="Nope",
                )
            assert error.value.code == code


async def test_start_succession_offers_top_candidate(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    team = await make_team(h, event)
    top = await h.user()
    other = await h.user()
    await add_member(h, event, team, top, willing=True, fit=0.9)
    await add_member(h, event, team, other, fit=0.5)

    offered, excluded, lead = await succession(h, team, event, set())
    assert offered == top.id
    assert excluded == []
    assert lead is None
    async with h.db() as session:
        offer = await session.scalar(
            select(models.LeadOffer).where(models.LeadOffer.project_id == team.id)
        )
        assert offer is not None
        assert offer.source == LeadOfferSource.SUCCESSION.value
        assert offer.tier == 2


async def test_start_succession_skips_excluded(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    team = await make_team(h, event)
    top = await h.user()
    other = await h.user()
    await add_member(h, event, team, top, willing=True, fit=0.9)
    await add_member(h, event, team, other, fit=0.5)

    offered, excluded, _ = await succession(h, team, event, {top.id})
    assert offered == other.id
    assert excluded == [str(top.id)]


@pytest.mark.parametrize("team_status", [TeamStatus.FORMING.value, TeamStatus.REVIEW.value])
async def test_succession_exhausted_keeps_role_empty(harness: Harness, team_status: str) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    empty = await make_team(h, event, team_status=team_status)
    assert await succession(h, empty, event, set()) == (None, [], None)

    team = await make_team(h, event, team_status=team_status)
    only = await h.user()
    await add_member(h, event, team, only, fit=0.5)
    offered, excluded, lead = await succession(h, team, event, {only.id})
    assert offered is None
    assert excluded == [str(only.id)]
    assert lead is None
    assert (
        await count(h, models.LeadOffer, models.LeadOffer.project_id.in_([empty.id, team.id])) == 0
    )


async def test_succession_forces_lead_in_locked_team(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    team = await make_team(h, event, team_status=TeamStatus.LOCKED.value)
    proposed = await h.user()
    strong = await h.user()
    weak = await h.user()
    await add_member(h, event, team, proposed, state="proposed", willing=True, fit=0.95)
    await add_member(h, event, team, strong, fit=0.6)
    await add_member(h, event, team, weak, fit=0.3)

    offered, excluded, lead = await succession(h, team, event, {proposed.id, strong.id, weak.id})
    assert offered is None
    assert lead == strong.id
    assert excluded == []
    async with h.db() as session:
        member = await session.scalar(
            select(models.ProjectMember).where(
                models.ProjectMember.project_id == team.id,
                models.ProjectMember.user_id == strong.id,
            )
        )
        assert member is not None
        assert member.role == "lead"
        alerts = list(
            await session.scalars(
                select(models.AdminAlert).where(
                    models.AdminAlert.type == "forced_lead",
                    models.AdminAlert.subject_id == team.id,
                )
            )
        )
        audits = list(
            await session.scalars(
                select(models.AuditLog).where(
                    models.AuditLog.entity_id == str(team.id),
                    models.AuditLog.action == "lead_assign",
                )
            )
        )
    assert [(alert.severity, alert.status) for alert in alerts] == [("info", "open")]
    assert [(audit.actor_kind, audit.actor_id) for audit in audits] == [("system", None)]
    for user in (proposed, strong, weak):
        assert (
            await count(
                h,
                models.Notification,
                models.Notification.user_id == user.id,
                models.Notification.type == "lead_assigned",
            )
            == 1
        )
    assert (
        await count(
            h,
            models.LeadOffer,
            models.LeadOffer.project_id == team.id,
            models.LeadOffer.status == LeadOfferStatus.PENDING.value,
        )
        == 0
    )


async def test_force_lead_waits_for_presence(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    team = await make_team(h, event, team_status=TeamStatus.LOCKED.value)
    first = await h.user()
    second = await h.user()
    await add_member(h, event, team, first, state="proposed", fit=0.9)
    await add_member(h, event, team, second, state="proposed", fit=0.4)
    async with h.db() as session:
        await session.execute(
            update(models.ProjectMember)
            .where(models.ProjectMember.project_id == team.id)
            .values(confirm_kind="presence", confirm_by=h.now + timedelta(hours=12))
        )
    assert await force(h, team, event) is None
    assert await count(h, models.AdminAlert, models.AdminAlert.subject_id == team.id) == 0
    async with h.db() as session:
        assert (await session.get_one(models.Project, team.id)).lead_user_id is None
        await session.execute(
            update(models.ProjectMember)
            .where(models.ProjectMember.user_id == second.id)
            .values(state="confirmed", confirmed_at=h.now, confirm_kind=None, confirm_by=None)
        )
    assert await force(h, team, event) == second.id


async def test_force_lead_guards(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    lead = await h.user()
    member = await h.user()
    for team_status in (TeamStatus.FORMING.value, TeamStatus.REVIEW.value):
        team = await make_team(h, event, team_status=team_status)
        user = await h.user()
        await add_member(h, event, team, user, fit=0.9)
        assert await force(h, team, event) is None
    led = await make_team(h, event, lead=lead, team_status=TeamStatus.LOCKED.value)
    await add_member(h, event, led, lead, role="lead")
    await add_member(h, event, led, member, fit=0.9)
    assert await force(h, led, event) is None
    standalone = await make_team(h, None, team_status=None)
    assert await force(h, standalone, None) is None


async def test_lead_succession_excluded_defaults_empty(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    team = await make_team(h, event)
    assert team.lead_succession_excluded == []
    async with h.db() as session:
        stored = await session.scalar(
            select(models.Project.lead_succession_excluded).where(models.Project.id == team.id)
        )
    assert stored == []


async def offer(
    h: Harness,
    project: models.Project,
    user: models.User,
    *,
    source: str = "transfer",
    offered_by: models.User | None = None,
    expires_in: timedelta = timedelta(hours=12),
    reason: str = "r",
) -> models.LeadOffer:
    async with h.db() as session:
        row = models.LeadOffer(
            project_id=project.id,
            event_id=project.event_id,
            user_id=user.id,
            source=source,
            offered_by=offered_by.id if offered_by is not None else None,
            tier=None,
            reason=reason,
            status=LeadOfferStatus.PENDING.value,
            expires_at=h.now + expires_in,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        return row


async def transfer_team(
    h: Harness, *, team_status: str = TeamStatus.FORMING.value
) -> tuple[models.Event, models.Project, models.User, models.User]:
    event = await make_hackathon(h)
    lead = await h.user()
    member = await h.user()
    team = await make_team(h, event, lead=lead, team_status=team_status)
    await add_member(h, event, team, lead, role="lead")
    await add_member(h, event, team, member)
    return event, team, lead, member


async def stored_offer(h: Harness, offer_id: UUID) -> models.LeadOffer:
    async with h.db() as session:
        return await session.get_one(models.LeadOffer, offer_id)


async def stored_project(h: Harness, project_id: UUID) -> models.Project:
    async with h.db() as session:
        return await session.get_one(models.Project, project_id)


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


async def pending_offers(h: Harness, project: models.Project) -> list[models.LeadOffer]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.LeadOffer).where(
                    models.LeadOffer.project_id == project.id,
                    models.LeadOffer.status == LeadOfferStatus.PENDING.value,
                )
            )
        )


async def row_counts(h: Harness) -> tuple[int, int, int]:
    return (
        await count(h, models.AuditLog, models.AuditLog.id.is_not(None)),
        await count(h, models.Notification, models.Notification.id.is_not(None)),
        len(await h.pending_jobs()),
    )


async def test_list_my_lead_offers(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    actor = await h.user()
    other = await h.user()
    first_team = await make_team(h, event)
    second_team = await make_team(h, event, team_status=TeamStatus.REVIEW.value)
    third_team = await make_team(h, event)
    await add_member(h, event, first_team, actor)
    await add_member(h, event, third_team, other)
    first = await offer(h, first_team, actor, source="succession")
    second = await offer(h, second_team, actor, source="formation")
    await offer(h, third_team, other, source="succession")

    async with await h.client(actor) as c:
        response = await c.get("/api/v1/me/lead-offers")
        assert response.status_code == 200, response.text
        items = response.json()
        assert [item["id"] for item in items] == [str(second.id), str(first.id)]
        assert items[0]["project"]["team_status"] == "review"
        assert items[0]["source"] == "formation"
        assert items[0]["tier"] is None
        assert items[0]["expires_at"] is not None
        assert all(item["allowed_actions"] == ["accept", "decline"] for item in items)
        accepted = await c.get("/api/v1/me/lead-offers", params={"status": "accepted"})
        assert accepted.json() == []
    async with await h.client(await h.user()) as c:
        assert (await c.get("/api/v1/me/lead-offers")).json() == []
    async with h.anonymous() as c:
        assert_problem(await c.get("/api/v1/me/lead-offers"), 401, ErrorCode.UNAUTHENTICATED)

    _, team, lead, member = await transfer_team(h)
    pending = await offer(h, team, member, offered_by=lead)
    async with h.db() as session:
        row = await session.get_one(models.Project, team.id)
        lead_row = await session.get_one(models.User, lead.id)
        view = await formation.team_view(session, row, lead_row, h.now)
    assert view.pending_lead_offer is not None
    assert view.pending_lead_offer.id == pending.id
    assert [action.value for action in view.pending_lead_offer.allowed_actions] == ["cancel"]
    async with await h.client(member) as c:
        items = (await c.get("/api/v1/me/lead-offers")).json()
    assert [(item["id"], item["allowed_actions"]) for item in items] == [
        (str(pending.id), ["accept", "decline"])
    ]


async def test_accept_transfer_offer(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event, team, lead, member = await transfer_team(h)
    third = await h.user()
    await add_member(h, event, team, third)
    before = await member_row(h, team, member)
    pending = await offer(h, team, member, offered_by=lead)

    async with await h.client(member) as c:
        response = await c.post(f"/api/v1/lead-offers/{pending.id}/accept")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "accepted"
    assert datetime.fromisoformat(body["responded_at"]) == h.now
    assert body["allowed_actions"] == []

    project = await stored_project(h, team.id)
    assert project.lead_user_id == member.id
    assert project.last_lead_action_at == h.now
    assert project.lead_succession_excluded == []
    previous = await member_row(h, team, lead)
    assert (previous.role, previous.state, previous.left_at) == ("member", "confirmed", None)
    promoted = await member_row(h, team, member)
    assert promoted.role == "lead"
    assert promoted.confirmed_at == before.confirmed_at
    async with h.db() as session:
        audits = list(
            await session.scalars(
                select(models.AuditLog).where(models.AuditLog.entity_id == str(team.id))
            )
        )
        notices = list(
            await session.scalars(
                select(models.Notification).where(models.Notification.type == "lead_transferred")
            )
        )
    assert [(a.action, a.actor_id, a.actor_kind) for a in audits] == [
        ("lead_transfer", member.id, "user")
    ]
    assert sorted(n.user_id for n in notices) == sorted([lead.id, member.id, third.id])
    for notice in notices:
        assert isinstance(notice.payload, dict)
        previous_lead = notice.payload["previous_lead"]
        assert isinstance(previous_lead, dict)
        assert previous_lead["user_id"] == str(lead.id)
    jobs = await h.pending_jobs()
    assert [job["task_name"] for job in jobs] == ["refresh_recommendations"]
    assert jobs[0]["args"]["project_id"] == str(team.id)


@pytest.mark.parametrize("team_status", [TeamStatus.FORMING.value, TeamStatus.REVIEW.value])
async def test_accept_succession_offer(harness: Harness, team_status: str) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    review_ends_at = h.now + timedelta(hours=6)
    team = await make_team(h, event, team_status=team_status, review_ends_at=review_ends_at)
    member = await h.user()
    other = await h.user()
    await add_member(h, event, team, member, state="proposed")
    await add_member(h, event, team, other)
    pending = await offer(h, team, member, source="succession")

    async with await h.client(member) as c:
        response = await c.post(f"/api/v1/lead-offers/{pending.id}/accept")
    assert response.status_code == 200, response.text
    promoted = await member_row(h, team, member)
    assert (promoted.role, promoted.state) == ("lead", "confirmed")
    assert promoted.confirm_by is None
    assert promoted.confirm_kind is None
    project = await stored_project(h, team.id)
    assert project.lead_user_id == member.id
    assert project.review_ends_at == review_ends_at
    assert (
        await count(
            h,
            models.AuditLog,
            models.AuditLog.entity_id == str(team.id),
            models.AuditLog.action == "lead_assign",
            models.AuditLog.actor_kind == "user",
        )
        == 1
    )
    async with h.db() as session:
        notices = list(
            await session.scalars(
                select(models.Notification).where(models.Notification.type == "lead_assigned")
            )
        )
    assert sorted(n.user_id for n in notices) == sorted([member.id, other.id])
    assert all(isinstance(n.payload, dict) and n.payload["reason"] for n in notices)


async def test_accept_lead_offer_gates(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event, team, lead, member = await transfer_team(h)
    pending = await offer(h, team, member, offered_by=lead)
    async with await h.client(lead) as c:
        response = await c.post(f"/api/v1/lead-offers/{pending.id}/accept")
        assert_problem(response, 403, ErrorCode.FORBIDDEN)
        response = await c.post(f"/api/v1/lead-offers/{uuid4()}/accept")
        assert_problem(response, 404, ErrorCode.NOT_FOUND)
    async with await h.client(member) as c:
        first = await c.post(f"/api/v1/lead-offers/{pending.id}/accept")
        assert first.status_code == 200, first.text
        counts = await row_counts(h)
        h.advance(timedelta(minutes=5))
        second = await c.post(f"/api/v1/lead-offers/{pending.id}/accept")
        assert second.status_code == 200, second.text
        assert second.json()["responded_at"] == first.json()["responded_at"]
        assert await row_counts(h) == counts

    declined_team = await make_team(h, event)
    declined_user = await h.user()
    await add_member(h, event, declined_team, declined_user)
    declined = await offer(h, declined_team, declined_user, source="succession")
    async with h.db() as session:
        await session.execute(
            update(models.LeadOffer)
            .where(models.LeadOffer.id == declined.id)
            .values(status="declined", responded_at=h.now)
        )
    async with await h.client(declined_user) as c:
        response = await c.post(f"/api/v1/lead-offers/{declined.id}/accept")
        assert_problem(response, 409, ErrorCode.INVALID_STATE)

    expired_team = await make_team(h, event)
    stale = await h.user()
    following = await h.user()
    await add_member(h, event, expired_team, stale, fit=0.9)
    await add_member(h, event, expired_team, following, fit=0.5)
    expired = await offer(
        h, expired_team, stale, source="succession", expires_in=-timedelta(minutes=1)
    )
    async with await h.client(stale) as c:
        response = await c.post(f"/api/v1/lead-offers/{expired.id}/accept")
        assert (
            assert_problem(response, 409, ErrorCode.INVALID_STATE).detail
            == "This lead offer has expired"
        )
    assert (await stored_offer(h, expired.id)).status == "expired"
    assert [(row.user_id, row.source) for row in await pending_offers(h, expired_team)] == [
        (following.id, "succession")
    ]

    led_team = await make_team(h, event, lead=lead)
    candidate = await h.user()
    await add_member(h, event, led_team, candidate)
    formation_offer = await offer(h, led_team, candidate, source="formation")
    async with await h.client(candidate) as c:
        response = await c.post(f"/api/v1/lead-offers/{formation_offer.id}/accept")
        assert (
            assert_problem(response, 409, ErrorCode.INVALID_STATE).detail
            == "This team already has a lead"
        )
    assert (await stored_offer(h, formation_offer.id)).status == "closed"

    left_team = await make_team(h, event)
    leaver = await h.user()
    await add_member(h, event, left_team, leaver)
    left_offer = await offer(h, left_team, leaver, source="succession")
    async with h.db() as session:
        await session.execute(
            update(models.ProjectMember)
            .where(models.ProjectMember.project_id == left_team.id)
            .values(left_at=h.now, left_reason="left")
        )
    async with await h.client(leaver) as c:
        response = await c.post(f"/api/v1/lead-offers/{left_offer.id}/accept")
        assert_problem(response, 409, ErrorCode.INVALID_STATE)

    finished_team = await make_team(h, event, team_status=TeamStatus.DISBANDED.value)
    finisher = await h.user()
    await add_member(h, event, finished_team, finisher)
    finished_offer = await offer(h, finished_team, finisher, source="succession")
    async with await h.client(finisher) as c:
        response = await c.post(f"/api/v1/lead-offers/{finished_offer.id}/accept")
        assert_problem(response, 409, ErrorCode.INVALID_STATE)


async def test_decline_transfer_offer(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    _, team, lead, member = await transfer_team(h)
    pending = await offer(h, team, member, offered_by=lead)
    async with await h.client(member) as c:
        response = await c.post(f"/api/v1/lead-offers/{pending.id}/decline")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "declined"
        counts = await row_counts(h)
        repeat = await c.post(f"/api/v1/lead-offers/{pending.id}/decline")
        assert repeat.status_code == 200, repeat.text
        assert await row_counts(h) == counts
    project = await stored_project(h, team.id)
    assert project.lead_user_id == lead.id
    assert project.lead_succession_excluded == []
    assert (await member_row(h, team, lead)).role == "lead"
    assert await pending_offers(h, team) == []
    async with h.db() as session:
        notices = list(
            await session.scalars(
                select(models.Notification).where(models.Notification.type == "lead_offer_declined")
            )
        )
    assert [n.user_id for n in notices] == [lead.id]
    assert isinstance(notices[0].payload, dict) and notices[0].payload["reason"]
    assert await count(h, models.AuditLog, models.AuditLog.id.is_not(None)) == 0
    assert await h.pending_jobs() == []

    async with await h.client(await h.user()) as c:
        response = await c.post(f"/api/v1/lead-offers/{pending.id}/decline")
        assert_problem(response, 403, ErrorCode.FORBIDDEN)
    async with h.db() as session:
        await session.execute(
            update(models.LeadOffer)
            .where(models.LeadOffer.id == pending.id)
            .values(status="expired")
        )
    async with await h.client(member) as c:
        response = await c.post(f"/api/v1/lead-offers/{pending.id}/decline")
        assert_problem(response, 409, ErrorCode.INVALID_STATE)
    late = await offer(h, team, member, offered_by=lead, expires_in=-timedelta(minutes=1))
    async with await h.client(member) as c:
        response = await c.post(f"/api/v1/lead-offers/{late.id}/decline")
        assert (
            assert_problem(response, 409, ErrorCode.INVALID_STATE).detail
            == "This lead offer has expired"
        )
    assert (await stored_offer(h, late.id)).status == "expired"


@pytest.mark.parametrize("team_status", [TeamStatus.LOCKED.value, TeamStatus.REVIEW.value])
async def test_decline_succession_offer(harness: Harness, team_status: str) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    team = await make_team(h, event, team_status=team_status)
    former = await h.user()
    strong = await h.user()
    weak = await h.user()
    await add_member(h, event, team, former, fit=0.2)
    await add_member(h, event, team, strong, fit=0.8)
    await add_member(h, event, team, weak, fit=0.5)
    offered, _, _ = await succession(h, team, event, {former.id})
    assert offered == strong.id
    [first] = await pending_offers(h, team)

    async with await h.client(strong) as c:
        response = await c.post(f"/api/v1/lead-offers/{first.id}/decline")
        assert response.status_code == 200, response.text
    [second] = await pending_offers(h, team)
    assert (second.user_id, second.source) == (weak.id, "succession")
    excluded = (await stored_project(h, team.id)).lead_succession_excluded
    assert sorted(str(value) for value in excluded) == sorted([str(former.id), str(strong.id)])

    async with await h.client(weak) as c:
        response = await c.post(f"/api/v1/lead-offers/{second.id}/decline")
        assert response.status_code == 200, response.text
    assert await pending_offers(h, team) == []
    project = await stored_project(h, team.id)
    alerts = await count(
        h,
        models.AdminAlert,
        models.AdminAlert.type == "forced_lead",
        models.AdminAlert.subject_id == team.id,
    )
    if team_status == TeamStatus.LOCKED.value:
        assert project.lead_user_id == strong.id
        assert project.lead_succession_excluded == []
        assert alerts == 1
    else:
        assert project.lead_user_id is None
        assert sorted(str(value) for value in project.lead_succession_excluded) == sorted(
            [str(former.id), str(strong.id), str(weak.id)]
        )
        assert alerts == 0


async def test_cancel_lead_offer(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    _, team, lead, member = await transfer_team(h)
    pending = await offer(h, team, member, offered_by=lead)
    async with await h.client(member) as c:
        response = await c.delete(f"/api/v1/lead-offers/{pending.id}")
        assert_problem(response, 403, ErrorCode.FORBIDDEN)
    h.advance(timedelta(minutes=1))
    async with await h.client(lead) as c:
        response = await c.delete(f"/api/v1/lead-offers/{pending.id}")
        assert response.status_code == 204, response.text
        cancelled = await stored_offer(h, pending.id)
        assert cancelled.status == "cancelled"
        assert cancelled.responded_at == h.now
        project = await stored_project(h, team.id)
        assert project.last_lead_action_at == h.now
        assert project.lead_user_id == lead.id
        assert await row_counts(h) == (0, 0, 0)
        h.advance(timedelta(minutes=1))
        repeat = await c.delete(f"/api/v1/lead-offers/{pending.id}")
        assert repeat.status_code == 204, repeat.text
        assert (await stored_offer(h, pending.id)).responded_at == cancelled.responded_at
        assert (await stored_project(h, team.id)).last_lead_action_at == cancelled.responded_at

        formation_offer = await offer(h, team, member, source="formation", offered_by=lead)
        response = await c.delete(f"/api/v1/lead-offers/{formation_offer.id}")
        assert_problem(response, 409, ErrorCode.INVALID_STATE)
        async with h.db() as session:
            await session.execute(
                update(models.LeadOffer)
                .where(models.LeadOffer.id == formation_offer.id)
                .values(status="closed", responded_at=h.now)
            )
        accepted = await offer(h, team, member, offered_by=lead)
        async with h.db() as session:
            await session.execute(
                update(models.LeadOffer)
                .where(models.LeadOffer.id == accepted.id)
                .values(status="accepted", responded_at=h.now)
            )
        response = await c.delete(f"/api/v1/lead-offers/{accepted.id}")
        assert_problem(response, 409, ErrorCode.INVALID_STATE)
        response = await c.delete(f"/api/v1/lead-offers/{uuid4()}")
        assert_problem(response, 404, ErrorCode.NOT_FOUND)


async def test_accept_standalone_transfer(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    lead = await h.user()
    member = await h.user()
    project = await make_team(h, None, lead=lead, team_status=None)
    async with h.db() as session:
        for user, role in ((lead, "lead"), (member, "member")):
            session.add(
                models.ProjectMember(
                    project_id=project.id,
                    event_id=None,
                    user_id=user.id,
                    role=role,
                    area_id=None,
                    state="confirmed",
                    trust="self",
                    joined_via="invite",
                    is_team_row=False,
                    joined_at=h.now,
                    confirmed_at=h.now,
                )
            )
    pending = await offer(h, project, member, offered_by=lead, expires_in=timedelta(days=7))
    async with await h.client(member) as c:
        response = await c.post(f"/api/v1/lead-offers/{pending.id}/accept")
    assert response.status_code == 200, response.text
    assert (await stored_project(h, project.id)).lead_user_id == member.id
    assert (await member_row(h, project, lead)).role == "member"
    assert (await member_row(h, project, member)).role == "lead"
    jobs = await h.pending_jobs()
    assert [job["task_name"] for job in jobs] == ["refresh_recommendations"]
    assert await count(h, models.Notification, models.Notification.id.is_not(None)) == 0


async def transfer(
    h: Harness, actor: models.User, project_id: UUID, user_id: UUID
) -> httpx.Response:
    async with await h.client(actor) as c:
        return await c.post(
            f"/api/v1/projects/{project_id}/transfer-lead", json={"user_id": str(user_id)}
        )


async def step(h: Harness, actor: models.User, project_id: UUID) -> httpx.Response:
    async with await h.client(actor) as c:
        return await c.post(f"/api/v1/projects/{project_id}/lead/step-down")


async def leave(h: Harness, actor: models.User, project_id: UUID) -> models.Project:
    async with h.db() as session:
        user = await session.get_one(models.User, actor.id)
        return await formation.leave_team(session, h.jobs, project_id, user, h.now)


async def leave_problem(h: Harness, actor: models.User, project_id: UUID) -> ErrorCode:
    with pytest.raises(ProblemError) as error:
        await leave(h, actor, project_id)
    return error.value.code


async def set_phase(h: Harness, event: models.Event, phase: str) -> None:
    async with h.db() as session:
        await session.execute(
            update(models.EventFormation)
            .where(models.EventFormation.event_id == event.id)
            .values(phase=phase)
        )


async def enrollment_of(h: Harness, event: models.Event, user: models.User) -> models.Enrollment:
    async with h.db() as session:
        row = await session.scalar(
            select(models.Enrollment).where(
                models.Enrollment.event_id == event.id, models.Enrollment.user_id == user.id
            )
        )
        assert row is not None
        return row


async def notified(h: Harness, kind: str) -> list[models.Notification]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.Notification).where(models.Notification.type == kind)
            )
        )


async def audits(h: Harness, project: models.Project) -> list[models.AuditLog]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.AuditLog).where(models.AuditLog.entity_id == str(project.id))
            )
        )


async def job_names(h: Harness) -> list[str]:
    return sorted(job["task_name"] for job in await h.pending_jobs())


async def lead_team(
    h: Harness,
    event: models.Event,
    *,
    team_status: str = TeamStatus.FORMING.value,
    lead_fit: float | None = None,
    **fields: Any,
) -> tuple[models.Project, models.User]:
    lead = await h.user()
    team = await make_team(h, event, lead=lead, team_status=team_status, **fields)
    await add_member(h, event, team, lead, role="lead", fit=lead_fit)
    return team, lead


async def test_transfer_lead_happy_path(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    team, lead = await lead_team(h, event, lead_fit=0.8)
    member = await h.user()
    await add_member(h, event, team, member, fit=0.7)

    response = await transfer(h, lead, team.id, member.id)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["source"] == "transfer"
    assert body["user_id"] == str(member.id)
    assert body["offered_by"] == str(lead.id)
    assert body["offerer"]["id"] == str(lead.id)
    assert body["tier"] == 4
    assert datetime.fromisoformat(body["expires_at"]) == h.now + timedelta(hours=12)
    assert body["status"] == "pending"
    assert body["allowed_actions"] == ["cancel"]
    assert await count(h, models.LeadOffer, models.LeadOffer.project_id == team.id) == 1
    project = await stored_project(h, team.id)
    assert project.last_lead_action_at == h.now
    assert project.lead_user_id == lead.id
    assert [n.user_id for n in await notified(h, "lead_offer")] == [member.id]
    assert await audits(h, team) == []
    assert await h.pending_jobs() == []
    async with await h.client(member) as c:
        [item] = (await c.get("/api/v1/me/lead-offers")).json()
    assert item["allowed_actions"] == ["accept", "decline"]


async def test_transfer_lead_caps_and_alerts(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    soon = await make_hackathon(h, starts_in=timedelta(hours=1))
    locked, lead = await lead_team(h, soon, team_status=TeamStatus.LOCKED.value)
    weak = await h.user()
    await add_member(h, soon, locked, weak, fit=0.4)
    response = await transfer(h, lead, locked.id, weak.id)
    assert response.status_code == 201, response.text
    assert datetime.fromisoformat(response.json()["expires_at"]) == h.now + timedelta(hours=1)
    async with h.db() as session:
        alerts = list(
            await session.scalars(
                select(models.AdminAlert).where(models.AdminAlert.type == "lead_below_threshold")
            )
        )
    assert [(a.severity, a.status, a.subject_id) for a in alerts] == [("info", "open", locked.id)]

    event = await make_hackathon(h)
    strong_team, strong_lead = await lead_team(h, event)
    strong = await h.user()
    await add_member(h, event, strong_team, strong, fit=0.8)
    response = await transfer(h, strong_lead, strong_team.id, strong.id)
    assert response.status_code == 201, response.text
    assert await count(h, models.AdminAlert, models.AdminAlert.subject_id == strong_team.id) == 0

    owner = await h.user()
    helper = await h.user()
    standalone = await make_team(h, None, lead=owner, team_status=None)
    async with h.db() as session:
        for user, role in ((owner, "lead"), (helper, "member")):
            session.add(
                models.ProjectMember(
                    project_id=standalone.id,
                    event_id=None,
                    user_id=user.id,
                    role=role,
                    area_id=None,
                    state="confirmed",
                    trust="self",
                    joined_via="invite",
                    is_team_row=False,
                    joined_at=h.now,
                    confirmed_at=h.now,
                )
            )
    notices = await count(h, models.Notification, models.Notification.id.is_not(None))
    response = await transfer(h, owner, standalone.id, helper.id)
    assert response.status_code == 201, response.text
    assert response.json()["tier"] is None
    assert datetime.fromisoformat(response.json()["expires_at"]) == h.now + timedelta(days=7)
    assert await count(h, models.Notification, models.Notification.id.is_not(None)) == notices + 1
    async with h.db() as session:
        [notice] = list(
            await session.scalars(
                select(models.Notification).where(
                    models.Notification.user_id == helper.id,
                    models.Notification.type == "project_lead_offer",
                )
            )
        )
        assert isinstance(notice.payload, dict)
        assert notice.payload["lead_offer_id"] == response.json()["id"]
    assert await count(h, models.AdminAlert, models.AdminAlert.subject_id == standalone.id) == 0


async def test_transfer_lead_gates(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    team, lead = await lead_team(h, event)
    member = await h.user()
    proposed = await h.user()
    other = await h.user()
    outsider = await h.user()
    await add_member(h, event, team, member)
    await add_member(h, event, team, proposed, state="proposed")
    await add_member(h, event, team, other)

    assert_problem(await transfer(h, member, team.id, other.id), 403, ErrorCode.FORBIDDEN)
    for target in (lead, outsider, proposed):
        response = await transfer(h, lead, team.id, target.id)
        assert_problem(response, 409, ErrorCode.INVALID_STATE)
    assert (await transfer(h, lead, team.id, member.id)).status_code == 201
    response = await transfer(h, lead, team.id, other.id)
    problem = assert_problem(response, 409, ErrorCode.INVALID_STATE)
    assert problem.detail == "A lead offer is already pending for this team"
    assert len(await pending_offers(h, team)) == 1
    assert_problem(await transfer(h, lead, uuid4(), member.id), 404, ErrorCode.NOT_FOUND)
    async with h.anonymous() as c:
        response = await c.post(
            f"/api/v1/projects/{team.id}/transfer-lead", json={"user_id": str(member.id)}
        )
        assert_problem(response, 401, ErrorCode.UNAUTHENTICATED)

    forming_event = await make_hackathon(h)
    busy, busy_lead = await lead_team(h, forming_event)
    busy_member = await h.user()
    await add_member(h, forming_event, busy, busy_member)
    await set_phase(h, forming_event, "forming")
    response = await transfer(h, busy_lead, busy.id, busy_member.id)
    assert_problem(response, 409, ErrorCode.FORMATION_IN_PROGRESS)

    cancelled_event = await make_hackathon(h)
    cancelled, cancelled_lead = await lead_team(
        h,
        cancelled_event,
        team_status=TeamStatus.CANCELLED.value,
        status=ProjectStatus.CANCELLED.value,
    )
    cancelled_member = await h.user()
    await add_member(h, cancelled_event, cancelled, cancelled_member)
    async with h.db() as session:
        await session.execute(
            update(models.Event)
            .where(models.Event.id == cancelled_event.id)
            .values(status=EventStatus.CANCELLED.value)
        )
    response = await transfer(h, cancelled_lead, cancelled.id, cancelled_member.id)
    assert_problem(response, 409, ErrorCode.INVALID_STATE)


async def test_transfer_lead_after_result(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    continued, lead = await lead_team(
        h, event, team_status=TeamStatus.FINISHED.value, status=ProjectStatus.ACTIVE.value
    )
    member = await h.user()
    await add_member(h, event, continued, member, fit=0.9)
    response = await transfer(h, lead, continued.id, member.id)
    assert response.status_code == 201, response.text
    assert datetime.fromisoformat(response.json()["expires_at"]) == h.now + timedelta(days=7)
    async with await h.client(member) as c:
        accepted = await c.post(f"/api/v1/lead-offers/{response.json()['id']}/accept")
    assert accepted.status_code == 200, accepted.text
    assert (await stored_project(h, continued.id)).lead_user_id == member.id

    abandoned, abandoned_lead = await lead_team(
        h, event, team_status=TeamStatus.FINISHED.value, status=ProjectStatus.ABANDONED.value
    )
    abandoned_member = await h.user()
    await add_member(h, event, abandoned, abandoned_member)
    response = await transfer(h, abandoned_lead, abandoned.id, abandoned_member.id)
    assert assert_problem(response, 409, ErrorCode.INVALID_STATE).detail == "This project is closed"
    assert_problem(await step(h, abandoned_lead, abandoned.id), 409, ErrorCode.INVALID_STATE)


async def test_transfer_then_accept(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    team, lead = await lead_team(h, event)
    member = await h.user()
    await add_member(h, event, team, member)
    response = await transfer(h, lead, team.id, member.id)
    async with await h.client(member) as c:
        accepted = await c.post(f"/api/v1/lead-offers/{response.json()['id']}/accept")
    assert accepted.status_code == 200, accepted.text
    assert (await member_row(h, team, lead)).role == "member"
    assert (await member_row(h, team, member)).role == "lead"
    assert (await stored_project(h, team.id)).lead_user_id == member.id


async def test_step_down(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    team, lead = await lead_team(h, event, ready_at=h.now - timedelta(hours=1))
    strong = await h.user()
    other = await h.user()
    await add_member(h, event, team, strong, fit=0.9)
    await add_member(h, event, team, other)
    old = await offer(h, team, other, offered_by=lead)

    response = await step(h, lead, team.id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["lead_user_id"] is None
    assert body["pending_lead_offer"] is None
    assert datetime.fromisoformat(body["last_lead_action_at"]) == h.now
    assert datetime.fromisoformat(body["ready_at"]) == h.now - timedelta(hours=1)
    [row] = [m for m in body["members"] if m["user_id"] == str(lead.id)]
    assert row["role"] == "member"
    assert row["left_at"] is None
    assert (await stored_offer(h, old.id)).status == "cancelled"
    [succession_offer] = await pending_offers(h, team)
    assert (succession_offer.user_id, succession_offer.source) == (strong.id, "succession")
    project = await stored_project(h, team.id)
    assert [str(value) for value in project.lead_succession_excluded] == [str(lead.id)]
    assert [(a.action, a.actor_id, a.actor_kind) for a in await audits(h, team)] == [
        ("lead_step_down", lead.id, "user")
    ]
    assert sorted(n.user_id for n in await notified(h, "lead_stepped_down")) == sorted(
        [strong.id, other.id]
    )
    assert await job_names(h) == ["refresh_recommendations"]


async def test_step_down_gates_and_repeat(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    team, lead = await lead_team(h, event, lead_fit=0.8)
    strong = await h.user()
    other = await h.user()
    await add_member(h, event, team, strong, fit=0.9)
    await add_member(h, event, team, other)
    assert_problem(await step(h, strong, team.id), 403, ErrorCode.FORBIDDEN)
    assert_problem(await step(h, lead, uuid4()), 404, ErrorCode.NOT_FOUND)

    assert (await step(h, lead, team.id)).status_code == 200
    assert_problem(await step(h, lead, team.id), 403, ErrorCode.FORBIDDEN)
    [first] = await pending_offers(h, team)
    assert first.user_id == strong.id
    async with await h.client(strong) as c:
        assert (await c.post(f"/api/v1/lead-offers/{first.id}/accept")).status_code == 200
    assert (await step(h, strong, team.id)).status_code == 200
    [second] = await pending_offers(h, team)
    assert second.user_id == lead.id
    project = await stored_project(h, team.id)
    assert [str(value) for value in project.lead_succession_excluded] == [str(strong.id)]

    forming_event = await make_hackathon(h)
    busy, busy_lead = await lead_team(h, forming_event)
    await set_phase(h, forming_event, "forming")
    assert_problem(await step(h, busy_lead, busy.id), 409, ErrorCode.FORMATION_IN_PROGRESS)

    finished, finished_lead = await lead_team(
        h, event, team_status=TeamStatus.FINISHED.value, status=ProjectStatus.ACTIVE.value
    )
    successor = await h.user()
    await add_member(h, event, finished, successor, fit=0.7)
    response = await step(h, finished_lead, finished.id)
    assert response.status_code == 200, response.text
    assert response.json()["lead_user_id"] is None
    [finished_offer] = await pending_offers(h, finished)
    assert finished_offer.user_id == successor.id


async def test_declined_planned_lead_passes_to_teammate(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    team = await make_team(h, event, team_status=TeamStatus.REVIEW.value)
    volunteer = await h.user()
    top = await h.user()
    rest = await h.user()
    await add_member(h, event, team, volunteer, willing=True, fit=0.3)
    await add_member(h, event, team, top, fit=0.9)
    await add_member(h, event, team, rest, fit=0.5)
    planned = await offer(
        h, team, volunteer, source="formation", reason="Planned lead of the new team"
    )
    async with await h.client(volunteer) as c:
        assert (await c.post(f"/api/v1/lead-offers/{planned.id}/decline")).status_code == 200
    [following] = await pending_offers(h, team)
    assert following.user_id == top.id
    async with await h.client(top) as c:
        response = await c.post(f"/api/v1/lead-offers/{following.id}/accept")
    assert response.status_code == 200, response.text
    assert (await stored_project(h, team.id)).lead_user_id == top.id
    kept = await member_row(h, team, volunteer)
    assert (kept.role, kept.state, kept.left_at) == ("member", "confirmed", None)
    assert [(a.action, a.actor_kind) for a in await audits(h, team)] == [("lead_assign", "user")]


async def test_leave_team_as_member(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    team, lead = await lead_team(h, event, team_status=TeamStatus.REVIEW.value)
    leaver = await h.user()
    other = await h.user()
    await add_member(h, event, team, leaver)
    await add_member(h, event, team, other)
    await set_phase(h, event, "review")

    project = await leave(h, leaver, team.id)
    assert project.seat_open_since is None
    row = await member_row(h, team, leaver)
    assert (row.left_at, row.left_reason) == (h.now, "left")
    enrollment = await enrollment_of(h, event, leaver)
    assert (enrollment.status, enrollment.reserve_rank, enrollment.status_at) == (
        "reserve",
        None,
        h.now,
    )
    notices = await notified(h, "team_member_left")
    assert sorted(n.user_id for n in notices) == sorted([lead.id, other.id])
    assert all(isinstance(n.payload, dict) and n.payload["member_count"] == 2 for n in notices)
    assert await job_names(h) == ["rank_reserve", "refresh_recommendations"]
    assert await audits(h, team) == []


async def test_leave_team_as_lead(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    team, lead = await lead_team(h, event, team_status=TeamStatus.LOCKED.value)
    member = await h.user()
    await add_member(h, event, team, member)
    await set_phase(h, event, "locked")

    await leave(h, lead, team.id)
    assert (await stored_project(h, team.id)).lead_user_id is None
    assert (await member_row(h, team, lead)).left_reason == "left"
    assert (await enrollment_of(h, event, lead)).status == "reserve"
    [succession_offer] = await pending_offers(h, team)
    assert succession_offer.user_id == member.id
    assert [(a.action, a.actor_id) for a in await audits(h, team)] == [("lead_step_down", lead.id)]
    assert [n.user_id for n in await notified(h, "lead_stepped_down")] == [member.id]
    assert await job_names(h) == ["rank_reserve", "refresh_recommendations"]


async def test_leave_forming_core_disbands(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    team, lead = await lead_team(h, event, ready_at=h.now)
    invitee = await h.user()
    requester = await h.user()
    await enroll(h, event, requester, status="active")
    async with h.db() as session:
        session.add(
            models.Invite(
                project_id=team.id,
                event_id=event.id,
                user_id=invitee.id,
                invited_by=lead.id,
                kind="core",
                status="pending",
                expires_at=h.now + timedelta(days=1),
            )
        )
        session.add(
            models.JoinRequest(
                project_id=team.id, event_id=event.id, user_id=requester.id, status="pending"
            )
        )

    await leave(h, lead, team.id)
    project = await stored_project(h, team.id)
    assert (project.team_status, project.status) == ("disbanded", "disbanded")
    assert project.lead_user_id is None
    assert project.ready_at is None
    async with h.db() as session:
        invite = await session.scalar(
            select(models.Invite).where(models.Invite.project_id == team.id)
        )
        request = await session.scalar(
            select(models.JoinRequest).where(models.JoinRequest.project_id == team.id)
        )
    assert invite is not None and (invite.status, invite.closed_reason) == (
        "closed",
        "team_disbanded",
    )
    assert request is not None and request.status == "closed"
    assert [a.action for a in await audits(h, team)].count("project_disband") == 1
    assert await count(h, models.LeadOffer, models.LeadOffer.project_id == team.id) == 0
    assert (await enrollment_of(h, event, lead)).status == "active"


async def test_leave_team_gates(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    team, lead = await lead_team(h, event)
    assert await leave_problem(h, await h.user(), team.id) == ErrorCode.FORBIDDEN
    standalone = await make_team(h, None, lead=lead, team_status=None)
    assert await leave_problem(h, lead, standalone.id) == ErrorCode.NOT_FOUND
    assert await leave_problem(h, lead, uuid4()) == ErrorCode.NOT_FOUND

    cancelled_event = await make_hackathon(h)
    cancelled, cancelled_lead = await lead_team(
        h,
        cancelled_event,
        team_status=TeamStatus.CANCELLED.value,
        status=ProjectStatus.CANCELLED.value,
    )
    async with h.db() as session:
        await session.execute(
            update(models.Event)
            .where(models.Event.id == cancelled_event.id)
            .values(status=EventStatus.CANCELLED.value)
        )
    assert await leave_problem(h, cancelled_lead, cancelled.id) == ErrorCode.INVALID_STATE

    await set_phase(h, event, "forming")
    assert await leave_problem(h, lead, team.id) == ErrorCode.FORMATION_IN_PROGRESS


async def test_leave_finished_team(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    team, lead = await lead_team(
        h, event, team_status=TeamStatus.FINISHED.value, status=ProjectStatus.SHIPPED.value
    )
    member = await h.user()
    other = await h.user()
    await add_member(h, event, team, member)
    await add_member(h, event, team, other, fit=0.8)
    await set_phase(h, event, "forming")
    before = await enrollment_of(h, event, member)

    await leave(h, member, team.id)
    row = await member_row(h, team, member)
    assert (row.left_at, row.left_reason) == (h.now, "left")
    after = await enrollment_of(h, event, member)
    assert (after.status, after.status_at) == (before.status, before.status_at)
    assert await audits(h, team) == []
    assert await job_names(h) == ["refresh_recommendations"]

    await leave(h, lead, team.id)
    assert (await stored_project(h, team.id)).lead_user_id is None
    assert [(a.action, a.actor_id) for a in await audits(h, team)] == [("lead_step_down", lead.id)]
    [succession_offer] = await pending_offers(h, team)
    assert succession_offer.user_id == other.id
    assert (await enrollment_of(h, event, lead)).status == "placed"


async def test_remove_banned_lead_from_review_team(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    team, lead = await lead_team(h, event, team_status=TeamStatus.REVIEW.value)
    member = await h.user()
    await add_member(h, event, team, member)
    await set_phase(h, event, "review")
    earlier = h.now - timedelta(hours=1)
    async with h.db() as session:
        await session.execute(
            update(models.Enrollment)
            .where(models.Enrollment.event_id == event.id, models.Enrollment.user_id == lead.id)
            .values(status="withdrawn", status_at=earlier)
        )
    admin = await h.admin()
    async with h.db() as session:
        project = await session.get_one(models.Project, team.id)
        loaded = await session.get_one(models.Event, event.id)
        row = await session.scalar(
            select(models.ProjectMember).where(
                models.ProjectMember.project_id == team.id,
                models.ProjectMember.user_id == lead.id,
            )
        )
        assert row is not None
        result = await formation.remove_member_from_team(
            session,
            project,
            loaded,
            row,
            h.now,
            left_reason=LeftReason.BANNED,
            notice_reason="The member was banned",
            audit_actor=admin.id,
            audit_kind=ActorKind.ADMIN,
        )
    assert result is None
    assert (await member_row(h, team, lead)).left_reason == "banned"
    enrollment = await enrollment_of(h, event, lead)
    assert (enrollment.status, enrollment.status_at) == ("withdrawn", earlier)
    assert [(a.action, a.actor_kind) for a in await audits(h, team)] == [
        ("lead_step_down", "admin")
    ]
    [succession_offer] = await pending_offers(h, team)
    assert succession_offer.user_id == member.id


async def test_concurrent_transfers(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    team, lead = await lead_team(h, event)
    first = await h.user()
    second = await h.user()
    await add_member(h, event, team, first)
    await add_member(h, event, team, second)
    results = await asyncio.gather(
        transfer(h, lead, team.id, first.id), transfer(h, lead, team.id, second.id)
    )
    assert sorted(response.status_code for response in results) == [201, 409]
    [failed] = [response for response in results if response.status_code == 409]
    problem = assert_problem(failed, 409, ErrorCode.INVALID_STATE)
    assert problem.detail == "A lead offer is already pending for this team"
    assert len(await pending_offers(h, team)) == 1
    assert len(await notified(h, "lead_offer")) == 1
    assert (await stored_project(h, team.id)).lead_user_id == lead.id


async def sweep(h: Harness, event: models.Event) -> int:
    async with h.db() as session:
        loaded = await session.get_one(models.Event, event.id)
        return await formation.expire_lead_offers(session, loaded, h.now)


async def forced_alerts(h: Harness, project: models.Project) -> list[models.AdminAlert]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.AdminAlert).where(
                    models.AdminAlert.type == "forced_lead",
                    models.AdminAlert.subject_id == project.id,
                )
            )
        )


async def succession_team(
    h: Harness, team_status: str, *, excluded: tuple[str, ...]
) -> tuple[models.Event, models.Project, dict[str, models.User]]:
    event = await make_hackathon(h)
    users = {name: await h.user() for name in ("a", "b", "c")}
    team = await make_team(
        h,
        event,
        team_status=team_status,
        lead_succession_excluded=[users[name].id for name in excluded],
    )
    for name, fit in (("a", 0.2), ("b", 0.8), ("c", 0.5)):
        await add_member(h, event, team, users[name], fit=fit)
    return event, team, users


async def test_expire_lead_offers_scoping(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    first_event = await make_hackathon(h)
    second_event = await make_hackathon(h)
    first_team, first_lead = await lead_team(h, first_event)
    second_team, second_lead = await lead_team(h, second_event)
    first_member = await h.user()
    second_member = await h.user()
    await add_member(h, first_event, first_team, first_member)
    await add_member(h, second_event, second_team, second_member)
    due = -timedelta(minutes=1)
    first = await offer(h, first_team, first_member, offered_by=first_lead, expires_in=due)
    second = await offer(h, second_team, second_member, offered_by=second_lead, expires_in=due)

    assert await sweep(h, first_event) == 1
    expired = await stored_offer(h, first.id)
    assert (expired.status, expired.responded_at) == ("expired", h.now)
    assert (await stored_offer(h, second.id)).status == "pending"
    updated_at = expired.updated_at
    assert await sweep(h, first_event) == 0
    assert (await stored_offer(h, first.id)).updated_at == updated_at


async def test_expire_lead_offers_advances_succession(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event, team, users = await succession_team(h, TeamStatus.LOCKED.value, excluded=("a",))
    due = await offer(h, team, users["b"], source="succession", expires_in=-timedelta(minutes=1))

    assert await sweep(h, event) == 1
    assert (await stored_offer(h, due.id)).status == "expired"
    project = await stored_project(h, team.id)
    assert sorted(str(value) for value in project.lead_succession_excluded) == sorted(
        [str(users["a"].id), str(users["b"].id)]
    )
    [following] = await pending_offers(h, team)
    assert (following.user_id, following.source) == (users["c"].id, "succession")
    assert following.expires_at == h.now + timedelta(hours=12)
    assert await forced_alerts(h, team) == []


async def test_expire_lead_offers_forces_locked_team(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event, team, users = await succession_team(h, TeamStatus.LOCKED.value, excluded=("a", "b"))
    due = await offer(h, team, users["c"], source="succession", expires_in=-timedelta(minutes=1))

    assert await sweep(h, event) == 1
    assert (await stored_offer(h, due.id)).status == "expired"
    project = await stored_project(h, team.id)
    assert project.lead_user_id == users["b"].id
    assert project.lead_succession_excluded == []
    assert sorted(n.user_id for n in await notified(h, "lead_assigned")) == sorted(
        user.id for user in users.values()
    )
    assert [(a.status, a.severity) for a in await forced_alerts(h, team)] == [("open", "info")]
    assert await pending_offers(h, team) == []


async def test_expire_lead_offers_keeps_review_team_leaderless(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event, team, users = await succession_team(h, TeamStatus.REVIEW.value, excluded=("a", "b"))
    due = await offer(h, team, users["c"], source="succession", expires_in=-timedelta(minutes=1))

    assert await sweep(h, event) == 1
    assert (await stored_offer(h, due.id)).status == "expired"
    assert (await stored_project(h, team.id)).lead_user_id is None
    assert await pending_offers(h, team) == []
    assert await forced_alerts(h, team) == []
    assert await notified(h, "lead_assigned") == []


async def test_expire_transfer_offer_changes_nothing(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    team, lead = await lead_team(h, event)
    member = await h.user()
    await add_member(h, event, team, member)
    due = await offer(h, team, member, offered_by=lead, expires_in=-timedelta(minutes=1))

    assert await sweep(h, event) == 1
    assert (await stored_offer(h, due.id)).status == "expired"
    project = await stored_project(h, team.id)
    assert project.lead_user_id == lead.id
    assert project.lead_succession_excluded == []
    assert (await member_row(h, team, lead)).role == "lead"
    assert await pending_offers(h, team) == []
    assert await count(h, models.Notification, models.Notification.id.is_not(None)) == 0
    assert await audits(h, team) == []


async def test_expire_lead_offers_skips_not_due(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    team, lead = await lead_team(h, event)
    member = await h.user()
    await add_member(h, event, team, member)
    later = await offer(h, team, member, offered_by=lead, expires_in=timedelta(minutes=1))
    assert await sweep(h, event) == 0
    assert (await stored_offer(h, later.id)).status == "pending"

    accepted_team, accepted_lead = await lead_team(h, event)
    accepted_member = await h.user()
    await add_member(h, event, accepted_team, accepted_member)
    accepted = await offer(
        h,
        accepted_team,
        accepted_member,
        offered_by=accepted_lead,
        expires_in=-timedelta(minutes=1),
    )
    async with h.db() as session:
        await session.execute(
            update(models.LeadOffer)
            .where(models.LeadOffer.id == accepted.id)
            .values(status="accepted", responded_at=h.now)
        )
    assert await sweep(h, event) == 0
    assert (await stored_offer(h, accepted.id)).status == "accepted"
    assert (await stored_project(h, accepted_team.id)).lead_user_id == accepted_lead.id


async def test_expire_lead_offer_of_departed_candidate(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event, team, users = await succession_team(h, TeamStatus.FORMING.value, excluded=())
    due = await offer(h, team, users["b"], source="succession", expires_in=-timedelta(minutes=1))
    async with h.db() as session:
        await session.execute(
            update(models.ProjectMember)
            .where(
                models.ProjectMember.project_id == team.id,
                models.ProjectMember.user_id == users["b"].id,
            )
            .values(left_at=h.now, left_reason="left")
        )

    assert await sweep(h, event) == 1
    assert (await stored_offer(h, due.id)).status == "expired"
    [following] = await pending_offers(h, team)
    assert following.user_id == users["c"].id


async def test_expire_lead_offer_on_closed_team(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event, team, users = await succession_team(h, TeamStatus.DISBANDED.value, excluded=())
    due = await offer(h, team, users["b"], source="succession", expires_in=-timedelta(minutes=1))

    assert await sweep(h, event) == 1
    assert (await stored_offer(h, due.id)).status == "expired"
    assert await pending_offers(h, team) == []
    assert (await stored_project(h, team.id)).lead_succession_excluded == []


async def test_expire_lead_offers_waits_for_presence(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h, starts_in=timedelta(hours=24))
    first = await h.user()
    second = await h.user()
    team = await make_team(
        h, event, team_status=TeamStatus.LOCKED.value, lead_succession_excluded=[second.id]
    )
    await add_member(h, event, team, first, state="proposed", fit=0.9)
    await add_member(h, event, team, second, state="proposed", fit=0.4)
    async with h.db() as session:
        await session.execute(
            update(models.ProjectMember)
            .where(models.ProjectMember.project_id == team.id)
            .values(confirm_kind="presence", confirm_by=h.now + timedelta(hours=12))
        )
    due = await offer(h, team, first, source="succession", expires_in=-timedelta(minutes=1))

    assert await sweep(h, event) == 1
    assert (await stored_offer(h, due.id)).status == "expired"
    assert (await stored_project(h, team.id)).lead_user_id is None
    assert await forced_alerts(h, team) == []

    async with h.db() as session:
        await session.execute(
            update(models.ProjectMember)
            .where(models.ProjectMember.user_id == second.id)
            .values(state="confirmed", confirmed_at=h.now, confirm_kind=None, confirm_by=None)
        )
    async with h.db() as session:
        project = await session.get_one(models.Project, team.id)
        loaded = await session.get_one(models.Event, event.id)
        forced = await formation.force_lead(
            session, project, loaded, h.now, reason="Presence check deadline passed"
        )
        assert forced is not None and forced.user_id == second.id
    assert (await stored_project(h, team.id)).lead_user_id == second.id
    assert len(await forced_alerts(h, team)) == 1


async def test_expire_lead_offers_repeat_is_noop(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event, team, users = await succession_team(h, TeamStatus.LOCKED.value, excluded=("a", "b"))
    await offer(h, team, users["c"], source="succession", expires_in=-timedelta(minutes=1))

    assert await sweep(h, event) == 1
    counts = (
        await row_counts(h),
        await count(h, models.LeadOffer, models.LeadOffer.project_id == team.id),
    )
    assert await sweep(h, event) == 0
    assert (
        await row_counts(h),
        await count(h, models.LeadOffer, models.LeadOffer.project_id == team.id),
    ) == counts
