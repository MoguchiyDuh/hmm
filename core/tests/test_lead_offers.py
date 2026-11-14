import itertools
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from harness import Harness
from hmm_contracts.common import (
    AreaImportance,
    ErrorCode,
    EventKind,
    EventOrigin,
    EventStatus,
    LeadOfferSource,
    LeadOfferStatus,
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
            status=ProjectStatus.IDEA.value,
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
    with pytest.raises(ProblemError) as error:
        await expiry_of(h, finished, event)
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
