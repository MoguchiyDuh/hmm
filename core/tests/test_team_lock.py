import itertools
from datetime import datetime, timedelta

from harness import Harness
from hmm_contracts.common import (
    AreaImportance,
    EventKind,
    EventOrigin,
    EventStatus,
    FormationPhase,
    LeadOfferSource,
    MemberState,
    TeamStatus,
)
from hmm_contracts.notifications import AlertKind
from hmm_core import models
from hmm_core.catalog import service as catalog
from hmm_core.formation import service as formation
from hmm_core.notifications.service import raise_alert
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
                title=f"Placement Hack {next(_titles)}",
                starts_at=h.now + starts_in,
                ends_at=h.now + starts_in + timedelta(days=2),
                timezone="UTC",
                registration_deadline=registration_deadline,
                team_size_min=team_size_min,
                team_size_max=team_size_max,
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
            reserve_rank=None,
        )
        session.add(enrollment)
        await session.flush()
        await session.refresh(enrollment)
        return enrollment


async def seed_project(
    h: Harness,
    event: models.Event,
    *,
    name: str = "Team",
    team_status: str = "review",
    origin: str = "claim",
    review_ends_at: datetime | None = None,
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
            lead_user_id=None,
            status="idea",
            team_status=team_status,
            origin=origin,
            ready_at=None,
            review_ends_at=review_ends_at,
            last_lead_action_at=None,
        )
        session.add(project)
        await session.flush()
        await session.refresh(project)
        return project


async def add_member(
    h: Harness,
    event: models.Event,
    project: models.Project,
    user: models.User,
    *,
    role: str = "member",
    state: str = "confirmed",
    joined_via: str = "formation",
    confirm_by: datetime | None = None,
    confirm_kind: str | None = None,
    area_key: str | None = None,
    set_lead: bool = False,
) -> models.ProjectMember:
    async with h.db() as session:
        area_id = (
            None
            if area_key is None
            else await session.scalar(select(models.Area.id).where(models.Area.key == area_key))
        )
        member = models.ProjectMember(
            project_id=project.id,
            event_id=event.id,
            user_id=user.id,
            role=role,
            area_id=area_id,
            state=state,
            trust="self",
            joined_via=joined_via,
            proposal_reason=None,
            confirm_by=confirm_by,
            confirm_kind=confirm_kind,
            is_team_row=True,
            joined_at=h.now,
            confirmed_at=h.now if state == "confirmed" else None,
        )
        session.add(member)
        if set_lead:
            await session.execute(
                update(models.Project)
                .where(models.Project.id == project.id)
                .values(lead_user_id=user.id)
            )
        await session.flush()
        await session.refresh(member)
        return member


async def seed_proposal(
    h: Harness,
    event: models.Event,
    project: models.Project,
    *,
    confirm_by: datetime,
    lead: bool = False,
) -> tuple[models.User, models.ProjectMember]:
    user = await h.user()
    await enroll(h, event, user)
    member = await add_member(
        h,
        event,
        project,
        user,
        role="lead" if lead else "member",
        state="proposed",
        confirm_by=confirm_by,
        confirm_kind="presence",
        set_lead=lead,
    )
    return user, member


async def run_lapses(h: Harness, event: models.Event, *, limit: int = 200) -> int:
    async with h.db() as session:
        locked = (
            await session.scalars(
                select(models.Event).where(models.Event.id == event.id).with_for_update()
            )
        ).one()
        return await formation.lapse_due_proposals(session, h.jobs, locked, h.now, limit=limit)


async def stored_member(h: Harness, member: models.ProjectMember) -> models.ProjectMember:
    async with h.db() as session:
        return await session.get_one(models.ProjectMember, member.id)


async def stored_project(h: Harness, project: models.Project) -> models.Project:
    async with h.db() as session:
        return await session.get_one(models.Project, project.id)


async def notices(h: Harness, kind: str = "placement_lapsed") -> list[models.Notification]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.Notification).where(models.Notification.type == kind)
            )
        )


async def audits(h: Harness) -> list[models.AuditLog]:
    async with h.db() as session:
        return list(await session.scalars(select(models.AuditLog)))


async def todo_jobs(h: Harness) -> list[str]:
    return sorted(job["task_name"] for job in await h.pending_jobs() if job["status"] == "todo")


async def pending_offers(h: Harness, project: models.Project) -> list[models.LeadOffer]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.LeadOffer).where(
                    models.LeadOffer.project_id == project.id, models.LeadOffer.status == "pending"
                )
            )
        )


async def test_due_proposal_lapse_and_repeat(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    reg_deadline = h.now + timedelta(days=25)
    event = await make_hackathon(h, registration_deadline=reg_deadline)
    team = await seed_project(h, event, team_status=TeamStatus.LOCKED.value)
    confirm_by = h.now - timedelta(hours=1)
    user, member = await seed_proposal(h, event, team, confirm_by=confirm_by)
    async with h.db() as session:
        project = await session.get_one(models.Project, team.id)
        offer = await formation.create_lead_offer(
            session,
            project,
            event,
            user.id,
            h.now,
            source=LeadOfferSource.SUCCESSION,
            reason="Pending lead offer",
            tier=4,
        )
        offer_id = offer.id

    assert await run_lapses(h, event) == 1
    row = await stored_member(h, member)
    assert (row.left_at, row.left_reason) == (h.now, "lapsed")
    async with h.db() as session:
        enrollment = await session.scalar(
            select(models.Enrollment).where(
                models.Enrollment.event_id == event.id, models.Enrollment.user_id == user.id
            )
        )
        assert enrollment is not None
        assert (enrollment.status, enrollment.reserve_rank, enrollment.status_at) == (
            "lapsed",
            None,
            h.now,
        )
        offer = await session.get_one(models.LeadOffer, offer_id)
        assert (offer.status, offer.responded_at) == ("closed", h.now)
    [notice] = await notices(h)
    assert notice.user_id == user.id
    assert isinstance(notice.payload, dict)
    assert datetime.fromisoformat(str(notice.payload["confirm_by"])) == confirm_by
    assert notice.payload["reason"] == "The confirmation deadline passed"
    assert datetime.fromisoformat(str(notice.payload["reg_deadline"])) == reg_deadline
    [audit] = await audits(h)
    assert (audit.action, audit.actor_kind, audit.actor_id, audit.entity_type, audit.entity_id) == (
        "system_transition",
        "system",
        None,
        "project_member",
        str(member.id),
    )
    assert audit.before == {"state": "proposed", "confirm_kind": "presence"}
    assert audit.after == {"left_reason": "lapsed", "enrollment_status": "lapsed"}
    assert await todo_jobs(h) == ["refresh_recommendations"]
    assert (await stored_project(h, team)).seat_open_since is None
    assert await notices(h, "seat_opened") == []

    assert await run_lapses(h, event) == 0
    assert len(await notices(h)) == 1
    assert len(await audits(h)) == 1
    assert await todo_jobs(h) == ["refresh_recommendations"]


async def test_future_and_review_proposals_untouched(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    locked = await seed_project(h, event, team_status="locked")
    review = await seed_project(h, event, name="Review")
    _, future = await seed_proposal(h, event, locked, confirm_by=h.now + timedelta(hours=1))
    _, overdue_review = await seed_proposal(h, event, review, confirm_by=h.now - timedelta(hours=1))

    assert await run_lapses(h, event) == 0
    for member in (future, overdue_review):
        row = await stored_member(h, member)
        assert (row.state, row.left_at) == (MemberState.PROPOSED.value, None)
    assert await notices(h) == []
    assert await audits(h) == []
    assert await todo_jobs(h) == []


async def set_review(h: Harness, event: models.Event, *, deadline: datetime | None = None) -> None:
    async with h.db() as session:
        await session.execute(
            update(models.EventFormation)
            .where(models.EventFormation.event_id == event.id)
            .values(phase=FormationPhase.REVIEW.value, review_ends_at=deadline or h.now)
        )


async def confirmed_member(
    h: Harness, event: models.Event, project: models.Project, *, lead: bool = False
) -> tuple[models.User, models.ProjectMember]:
    user = await h.user()
    await enroll(h, event, user)
    return user, await add_member(
        h, event, project, user, role="lead" if lead else "member", set_lead=lead
    )


async def run_locks(h: Harness, event: models.Event, *, limit: int = 200) -> int:
    async with h.db() as session:
        locked_event = (
            await session.scalars(
                select(models.Event).where(models.Event.id == event.id).with_for_update()
            )
        ).one()
        return await formation.lock_due_teams(session, h.jobs, locked_event, h.now, limit=limit)


async def event_phase(h: Harness, event: models.Event) -> str:
    async with h.db() as session:
        return (await session.get_one(models.EventFormation, event.id)).phase


async def alerts(h: Harness, kind: str) -> list[models.AdminAlert]:
    async with h.db() as session:
        return list(
            await session.scalars(select(models.AdminAlert).where(models.AdminAlert.type == kind))
        )


async def seed_pending_work(
    h: Harness,
    event: models.Event,
    team: models.Project,
    other: models.Project,
    member: models.User,
    other_member: models.User,
) -> tuple[
    models.Invite, models.JoinRequest, models.SwapRequest, models.MergeProposal, models.LeadOffer
]:
    invitee = await h.user()
    applicant = await h.user()
    async with h.db() as session:
        invite = models.Invite(
            project_id=team.id,
            event_id=event.id,
            user_id=invitee.id,
            invited_by=member.id,
            kind="fill",
            status="pending",
            expires_at=h.now + timedelta(days=1),
        )
        request = models.JoinRequest(
            project_id=team.id,
            event_id=event.id,
            user_id=applicant.id,
            status="pending",
        )
        swap = models.SwapRequest(
            event_id=event.id,
            from_team_id=team.id,
            from_user_id=member.id,
            to_team_id=other.id,
            to_user_id=other_member.id,
            requested_by=member.id,
            status="pending",
        )
        merge = models.MergeProposal(
            event_id=event.id,
            absorbed_team_id=other.id,
            target_team_id=team.id,
            proposed_by="lead",
            status="pending",
        )
        session.add_all([invite, request, swap, merge])
        project = await session.get_one(models.Project, team.id)
        offer = await formation.create_lead_offer(
            session,
            project,
            event,
            member.id,
            h.now,
            source=LeadOfferSource.FORMATION,
            reason="Formation lead proposal",
            tier=4,
        )
        await session.flush()
        for row in (invite, request, swap, merge):
            await session.refresh(row)
        return invite, request, swap, merge, offer


async def test_main_round_locks_and_handoff(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    await set_review(h, event)
    first = await seed_project(h, event)
    second = await seed_project(h, event, name="Handoff")
    first_lead, first_lead_row = await confirmed_member(h, event, first, lead=True)
    first_mate, first_mate_row = await confirmed_member(h, event, first)
    silent, silent_row = await seed_proposal(h, event, first, confirm_by=h.now)
    old_lead, old_lead_row = await seed_proposal(h, event, second, confirm_by=h.now, lead=True)
    async with h.db() as session:
        await session.execute(
            update(models.ProjectMember)
            .where(models.ProjectMember.id == old_lead_row.id)
            .values(confirm_kind="reconfirm")
        )
    second_a, second_a_row = await confirmed_member(h, event, second)
    second_b, second_b_row = await confirmed_member(h, event, second)

    assert await run_locks(h, event) == 2
    for team in (first, second):
        row = await stored_project(h, team)
        assert (row.team_status, row.status, row.locked_at) == ("locked", "active", h.now)
        assert row.seat_open_since is None
    assert await notices(h, "seat_opened") == []
    for member in (silent_row, old_lead_row):
        assert (await stored_member(h, member)).left_reason == "lapsed"
    for member in (first_lead_row, first_mate_row, second_a_row, second_b_row):
        assert (await stored_member(h, member)).left_at is None
    assert {n.user_id for n in await notices(h)} == {silent.id, old_lead.id}
    new_lead = (await stored_project(h, second)).lead_user_id
    assert new_lead in {second_a.id, second_b.id}
    assert {n.user_id for n in await notices(h, "lead_assigned")} == {second_a.id, second_b.id}
    forced = await alerts(h, "forced_lead")
    assert len(forced) == 1
    assert (forced[0].status, forced[0].severity) == ("open", "info")
    locked_notices = await notices(h, "team_locked")
    assert {n.user_id for n in locked_notices} == {
        first_lead.id,
        first_mate.id,
        second_a.id,
        second_b.id,
    }
    for notice in locked_notices:
        assert isinstance(notice.payload, dict)
        assert notice.payload["lead"]
        assert datetime.fromisoformat(str(notice.payload["starts_at"])) == event.starts_at
        assert notice.payload["reserve_count"] == 0
    logs = await audits(h)
    assert len([a for a in logs if a.action == "lead_step_down" and a.actor_kind == "system"]) == 1
    assert len([a for a in logs if a.action == "lead_assign" and a.actor_kind == "system"]) == 1
    assert (
        len([a for a in logs if a.action == "system_transition" and a.entity_type == "project"])
        == 2
    )
    assert (
        len([a for a in logs if a.action == "system_transition" and a.entity_type == "formation"])
        == 1
    )
    assert await event_phase(h, event) == "locked"
    assert await todo_jobs(h) == ["refresh_recommendations", "refresh_recommendations"]
    before = (len(logs), len(locked_notices), len(forced), await todo_jobs(h))
    assert await run_locks(h, event) == 0
    assert (
        len(await audits(h)),
        len(await notices(h, "team_locked")),
        len(await alerts(h, "forced_lead")),
        await todo_jobs(h),
    ) == before


async def test_lock_preserves_future_proposal(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    await set_review(h, event)
    team = await seed_project(h, event, review_ends_at=h.now)
    await confirmed_member(h, event, team, lead=True)
    deadline = h.now + timedelta(hours=1)
    user, member = await seed_proposal(h, event, team, confirm_by=deadline)

    assert await run_locks(h, event) == 1
    row = await stored_member(h, member)
    assert (row.state, row.confirm_by, row.left_at) == ("proposed", deadline, None)
    assert (await stored_project(h, team)).team_status == "locked"
    assert not any(n.user_id == user.id for n in await notices(h))


async def test_per_team_lock_deadlines(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    await set_review(h, event)
    first = await seed_project(h, event)
    later = await seed_project(h, event, name="Later", review_ends_at=h.now + timedelta(hours=2))
    await confirmed_member(h, event, first, lead=True)
    later_lead, later_row = await confirmed_member(h, event, later, lead=True)

    assert await run_locks(h, event) == 1
    assert (await stored_project(h, first)).team_status == "locked"
    assert (await stored_project(h, later)).team_status == "review"
    assert await event_phase(h, event) == "locked"
    h.advance(timedelta(hours=2))
    assert await run_locks(h, event) == 1
    assert (await stored_project(h, later)).team_status == "locked"
    assert (await stored_member(h, later_row)).left_at is None
    assert not any(n.user_id == later_lead.id for n in await notices(h, "team_locked"))
    assert len([a for a in await audits(h) if a.entity_type == "formation"]) == 1


async def test_lock_batch_defers_phase_transition(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    await set_review(h, event)
    first = await seed_project(h, event)
    second = await seed_project(h, event, name="Second")
    await confirmed_member(h, event, first, lead=True)
    await confirmed_member(h, event, second, lead=True)
    async with h.db() as session:
        await session.execute(
            update(models.Project)
            .where(models.Project.id == first.id)
            .values(created_at=h.now - timedelta(hours=1))
        )
        await session.execute(
            update(models.Project).where(models.Project.id == second.id).values(created_at=h.now)
        )

    assert await run_locks(h, event, limit=1) == 1
    assert (await stored_project(h, first)).team_status == "locked"
    assert (await stored_project(h, second)).team_status == "review"
    assert await event_phase(h, event) == "review"
    assert not any(a.entity_type == "formation" for a in await audits(h))
    assert await run_locks(h, event, limit=1) == 1
    assert (await stored_project(h, second)).team_status == "locked"
    assert await event_phase(h, event) == "locked"
    assert len([a for a in await audits(h) if a.entity_type == "formation"]) == 1


async def test_disband_future_proposals_returns_reserve(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    await set_review(h, event)
    team = await seed_project(h, event)
    other = await seed_project(h, event, name="Other", team_status="locked")
    lead, lead_row = await seed_proposal(
        h, event, team, confirm_by=h.now + timedelta(hours=1), lead=True
    )
    mate, mate_row = await seed_proposal(h, event, team, confirm_by=h.now + timedelta(hours=1))
    other_lead, other_row = await confirmed_member(h, event, other, lead=True)
    pending = await seed_pending_work(h, event, team, other, lead, other_lead)
    async with h.db() as session:
        await session.execute(
            update(models.Project).where(models.Project.id == team.id).values(ready_at=h.now)
        )

    assert await run_locks(h, event) == 0
    project = await stored_project(h, team)
    assert (project.team_status, project.status, project.lead_user_id, project.ready_at) == (
        "disbanded",
        "disbanded",
        None,
        None,
    )
    for member in (lead_row, mate_row):
        assert (await stored_member(h, member)).left_reason == "disbanded"
    async with h.db() as session:
        enrollments = list(
            await session.scalars(
                select(models.Enrollment).where(models.Enrollment.user_id.in_([lead.id, mate.id]))
            )
        )
        assert all(
            (row.status, row.reserve_rank, row.status_at) == ("reserve", None, h.now)
            for row in enrollments
        )
        for item in pending:
            row = await session.get_one(type(item), item.id)
            assert row.status == "closed"
        invite = await session.get_one(models.Invite, pending[0].id)
        assert invite.closed_reason == "team_disbanded"
    assert {n.user_id for n in await notices(h, "team_disbanded")} == {lead.id, mate.id}
    assert (
        len(
            [
                a
                for a in await audits(h)
                if a.action == "project_disband" and a.actor_kind == "system"
            ]
        )
        == 1
    )
    assert await notices(h, "team_locked") == []
    assert await todo_jobs(h) == ["rank_reserve", "refresh_recommendations"]
    assert (await stored_member(h, other_row)).left_at is None


async def test_disband_overdue_proposals_stay_lapsed(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    await set_review(h, event)
    team = await seed_project(h, event)
    lead, member = await seed_proposal(h, event, team, confirm_by=h.now, lead=True)

    assert await run_locks(h, event) == 0
    assert (await stored_project(h, team)).team_status == "disbanded"
    assert (await stored_member(h, member)).left_reason == "lapsed"
    async with h.db() as session:
        enrollment = await session.scalar(
            select(models.Enrollment).where(models.Enrollment.user_id == lead.id)
        )
        assert enrollment is not None and enrollment.status == "lapsed"
    assert await notices(h, "team_disbanded") == []
    assert len(await notices(h)) == 1
    assert await todo_jobs(h) == ["rank_reserve"]


async def test_lock_closes_only_touching_pending_work(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    await set_review(h, event)
    team = await seed_project(h, event, review_ends_at=h.now)
    other = await seed_project(h, event, name="Other", review_ends_at=h.now + timedelta(days=1))
    third = await seed_project(h, event, name="Third", review_ends_at=h.now + timedelta(days=1))
    lead, lead_row = await confirmed_member(h, event, team, lead=True)
    other_lead, other_row = await confirmed_member(h, event, other, lead=True)
    third_lead, third_row = await confirmed_member(h, event, third, lead=True)
    touching = await seed_pending_work(h, event, team, other, lead, other_lead)
    untouched = await seed_pending_work(h, event, other, third, other_lead, third_lead)

    assert await run_locks(h, event) == 1
    async with h.db() as session:
        for item in touching:
            assert (await session.get_one(type(item), item.id)).status == "closed"
        for item in untouched:
            assert (await session.get_one(type(item), item.id)).status == "pending"
        assert (await session.get_one(models.Invite, touching[0].id)).closed_reason == "team_locked"
    project = await stored_project(h, team)
    assert (project.review_ends_at, project.locked_at) == (h.now, h.now)
    for row in (lead_row, other_row, third_row):
        assert (await stored_member(h, row)).left_at is None


async def test_lock_leaderless_team_forces_after_lock(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    await set_review(h, event)
    team = await seed_project(h, event)
    member, row = await confirmed_member(h, event, team)

    assert await run_locks(h, event) == 1
    project = await stored_project(h, team)
    assert (project.team_status, project.lead_user_id) == ("locked", member.id)
    assert (await stored_member(h, row)).role == "lead"
    assert (
        len([a for a in await audits(h) if a.action == "lead_assign" and a.actor_kind == "system"])
        == 1
    )
    [alert] = await alerts(h, "forced_lead")
    assert (alert.status, alert.severity) == ("open", "info")


async def test_lock_size_alerts(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h, team_size_min=3, team_size_max=3)
    await set_review(h, event)
    team = await seed_project(h, event)
    await confirmed_member(h, event, team, lead=True)
    await confirmed_member(h, event, team)

    assert await run_locks(h, event) == 1
    [alert] = await alerts(h, "team_below_min")
    assert (alert.status, alert.severity, alert.dedupe_key, alert.subject_id) == (
        "open",
        "warning",
        f"team_below_min:{team.id}",
        team.id,
    )

    with_reserve = await make_hackathon(h, team_size_min=3, team_size_max=3)
    await set_review(h, with_reserve)
    small = await seed_project(h, with_reserve, name="Reserve available")
    await confirmed_member(h, with_reserve, small, lead=True)
    await confirmed_member(h, with_reserve, small)
    await enroll(h, with_reserve, await h.user(), status="reserve")
    assert await run_locks(h, with_reserve) == 1
    assert len(await alerts(h, "team_below_min")) == 1

    viable_event = await make_hackathon(h, team_size_min=2, team_size_max=2)
    await set_review(h, viable_event)
    viable = await seed_project(h, viable_event, name="Viable")
    await confirmed_member(h, viable_event, viable, lead=True)
    await confirmed_member(h, viable_event, viable)
    async with h.db() as session:
        formation_row = await session.get_one(models.EventFormation, viable_event.id)
        await raise_alert(
            session,
            AlertKind.FORMATION_UNDERSIZED,
            h.now,
            title="Undersized",
            message="Below viable size",
            link=f"/admin/projects/{viable.id}",
            subject_type="project",
            subject_id=viable.id,
            subject_title=viable.name,
            dedupe_key=f"formation_undersized:{viable.id}:{formation_row.rounds}",
        )
    assert await run_locks(h, viable_event) == 1
    [resolved] = await alerts(h, "formation_undersized")
    assert resolved.status == "resolved"
    assert resolved.resolution_note == "The team reached the viable size"


async def test_lead_lapse_starts_succession(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    team = await seed_project(h, event, team_status="locked")
    lead, _ = await seed_proposal(h, event, team, confirm_by=h.now - timedelta(hours=1), lead=True)
    confirmed = await h.user()
    await enroll(h, event, confirmed)
    await add_member(h, event, team, confirmed)

    assert await run_lapses(h, event) == 1
    project = await stored_project(h, team)
    assert project.lead_user_id is None
    assert project.last_lead_action_at == h.now
    assert [str(value) for value in project.lead_succession_excluded] == [str(lead.id)]
    [offer] = await pending_offers(h, team)
    assert (offer.source, offer.user_id) == ("succession", confirmed.id)
    step_down = [audit for audit in await audits(h) if audit.action == "lead_step_down"]
    assert len(step_down) == 1
    assert (step_down[0].actor_kind, step_down[0].actor_id) == ("system", None)


async def test_all_overdue_members_lapse_without_forced_lead(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    team = await seed_project(h, event, team_status="locked")
    lead, lead_row = await seed_proposal(
        h, event, team, confirm_by=h.now - timedelta(hours=2), lead=True
    )
    mate, mate_row = await seed_proposal(h, event, team, confirm_by=h.now - timedelta(hours=1))

    assert await run_lapses(h, event) == 2
    assert (await stored_project(h, team)).lead_user_id is None
    for member in (lead_row, mate_row):
        assert (await stored_member(h, member)).left_reason == "lapsed"
    assert await pending_offers(h, team) == []
    assert {notice.user_id for notice in await notices(h)} == {lead.id, mate.id}
    assert not any(audit.action == "lead_assign" for audit in await audits(h))


async def test_lapses_across_teams(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    first = await seed_project(h, event, team_status="locked")
    second = await seed_project(h, event, name="Second", team_status="locked")
    _, a = await seed_proposal(h, event, first, confirm_by=h.now - timedelta(hours=1))
    _, b = await seed_proposal(h, event, second, confirm_by=h.now - timedelta(hours=1))
    _, future = await seed_proposal(h, event, second, confirm_by=h.now + timedelta(hours=1))

    assert await run_lapses(h, event) == 2
    for member in (a, b):
        assert (await stored_member(h, member)).left_at == h.now
    assert (await stored_member(h, future)).left_at is None
    assert await todo_jobs(h) == ["refresh_recommendations", "refresh_recommendations"]


async def test_lapses_bounded_and_ordered(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    team = await seed_project(h, event, team_status="locked")
    _, later = await seed_proposal(h, event, team, confirm_by=h.now)
    _, earlier = await seed_proposal(h, event, team, confirm_by=h.now - timedelta(hours=1))
    _, tied = await seed_proposal(h, event, team, confirm_by=h.now - timedelta(hours=1))
    expected = sorted([later, earlier, tied], key=lambda member: (member.confirm_by, member.id))

    assert await run_lapses(h, event, limit=0) == 0
    for index, member in enumerate(expected):
        assert await run_lapses(h, event, limit=1) == 1
        assert (await stored_member(h, member)).left_at == h.now
        for pending in expected[index + 1 :]:
            assert (await stored_member(h, pending)).left_at is None
    assert await run_lapses(h, event, limit=1) == 0
    async with h.db() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(models.ProjectMember)
                .where(
                    models.ProjectMember.project_id == team.id,
                    models.ProjectMember.left_at.is_not(None),
                )
            )
            == 3
        )


async def test_shared_lapse_without_succession(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    team = await seed_project(h, event)
    lead, member = await seed_proposal(
        h, event, team, confirm_by=h.now - timedelta(hours=1), lead=True
    )
    mate = await h.user()
    await enroll(h, event, mate)
    await add_member(h, event, team, mate)
    async with h.db() as session:
        locked_event = (
            await session.scalars(
                select(models.Event).where(models.Event.id == event.id).with_for_update()
            )
        ).one()
        project = (
            await session.scalars(
                select(models.Project).where(models.Project.id == team.id).with_for_update()
            )
        ).one()
        row = await session.get_one(models.ProjectMember, member.id)
        await formation._confirm_deadline_lapse(
            session,
            project,
            locked_event,
            row,
            h.now,
            timing=formation.formation_timing(
                locked_event, await formation.load_settings(session), h.now
            ),
            reason="Review ended",
            succession=False,
        )
    assert (await stored_project(h, team)).lead_user_id is None
    assert await pending_offers(h, team) == []
    assert [notice.user_id for notice in await notices(h)] == [lead.id]
    assert await todo_jobs(h) == []
