import copy
import json
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from harness import Harness
from hmm_contracts.common import (
    AttendanceRole,
    AttendanceStatus,
    JoinedVia,
    MemberRole,
    MemberState,
    ProjectOrigin,
    ProjectStatus,
    ProjectVisibility,
    TeamStatus,
    Trust,
)
from hmm_core import models
from sqlalchemy import func, select

IMPORT_URL = "/api/v1/internal/events/import"
HACKATHON = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "contracts"
        / "fixtures"
        / "imports"
        / "hackathon.json"
    ).read_text()
)


def hackathon(**overrides: Any) -> dict[str, Any]:
    item = copy.deepcopy(HACKATHON)
    item.update(overrides)
    return item


async def import_one(h: Harness, item: dict[str, Any]) -> dict[str, Any]:
    async with h.parser() as client:
        response: httpx.Response = await client.post(IMPORT_URL, json={"items": [item]})
    assert response.status_code == 200, response.text
    return response.json()["results"][0]


async def count(h: Harness, model: Any, *where: Any) -> int:
    async with h.db() as session:
        return await session.scalar(select(func.count()).select_from(model).where(*where)) or 0


async def members(h: Harness) -> tuple[models.User, models.User]:
    member_a = await h.user(login="mira-tidal")
    member_b = await h.user(login="owen-reed")
    telegram_id = await h.link_telegram(member_b, telegram_user_id=777_001)
    async with h.db() as session:
        link = await session.scalar(
            select(models.TelegramLink).where(models.TelegramLink.telegram_user_id == telegram_id)
        )
        assert link is not None
        link.username = "owen_reed_dev"
    return member_a, member_b


async def attendance_of(h: Harness, event_id: str, user: models.User) -> models.EventAttendance:
    async with h.db() as session:
        row = await session.scalar(
            select(models.EventAttendance).where(
                models.EventAttendance.event_id == UUID(event_id),
                models.EventAttendance.user_id == user.id,
            )
        )
        assert row is not None
        return row


async def system_confirmations(h: Harness, attendance_id: UUID) -> list[models.Confirmation]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.Confirmation).where(
                    models.Confirmation.fact_type == "attendance",
                    models.Confirmation.fact_id == attendance_id,
                )
            )
        )


async def add_attendance(
    h: Harness, event_id: str, user: models.User, status: AttendanceStatus, role: AttendanceRole
) -> None:
    async with h.db() as session:
        session.add(
            models.EventAttendance(
                event_id=UUID(event_id),
                user_id=user.id,
                status=status.value,
                role=role.value,
                proof_links=[],
                trust=Trust.SELF.value,
                status_at=h.now - timedelta(days=10),
            )
        )


async def add_team(
    h: Harness, event_id: str, users: list[models.User], place_status: str | None
) -> UUID:
    async with h.db() as session:
        team = models.Project(
            event_id=UUID(event_id),
            name=f"Team {len(users)} {place_status}",
            description_md="Team description",
            goal="Build together",
            links=[],
            visibility=ProjectVisibility.PUBLIC,
            status=ProjectStatus.ACTIVE,
            team_status=TeamStatus.FINISHED.value,
            origin=ProjectOrigin.ADMIN,
        )
        session.add(team)
        await session.flush()
        session.add_all(
            models.ProjectMember(
                project_id=team.id,
                event_id=UUID(event_id),
                user_id=user.id,
                role=MemberRole.MEMBER,
                state=MemberState.CONFIRMED,
                trust=Trust.SELF,
                joined_via=JoinedVia.ADMIN,
                is_team_row=True,
                joined_at=h.now - timedelta(days=10),
            )
            for user in users
        )
        if place_status is not None:
            await add_result(session, team.id, place_status)
        return team.id


async def add_result(session: Any, project_id: UUID, place_status: str) -> None:
    session.add(
        models.ProjectResult(
            project_id=project_id,
            outcome="finished",
            place=None,
            place_status=place_status,
            member_areas={},
            participants=[],
        )
    )


async def result_of(h: Harness, project_id: UUID) -> models.ProjectResult:
    async with h.db() as session:
        row = await session.get(models.ProjectResult, project_id)
        assert row is not None
        return row


def winner(login: str, place: int | None) -> dict[str, Any]:
    return {"github_login": login, "display_name": login.title(), "role": "winner", "place": place}


async def test_people_match_github_then_telegram(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    member_a, member_b = await members(h)
    result = await import_one(h, hackathon())
    assert result["result"] == "created"
    event_id = result["event_id"]
    async with h.db() as session:
        rows = {
            row.display_name: row
            for row in await session.scalars(
                select(models.EventSourcePerson).where(
                    models.EventSourcePerson.event_id == UUID(event_id)
                )
            )
        }
    assert len(rows) == 4
    assert rows["Mira Vale"].matched_user_id == member_a.id
    assert rows["Owen Reed"].matched_user_id == member_b.id
    assert rows["Sana Brook"].matched_user_id is None
    assert rows["Leo Ashford"].matched_user_id is None
    for user in (member_a, member_b):
        attendance = await attendance_of(h, event_id, user)
        assert attendance.status == "attended"
        assert attendance.role == "attendee"
        assert attendance.trust == "trusted"
        confirmations = await system_confirmations(h, attendance.id)
        assert [(row.source, row.confirmer_id) for row in confirmations] == [("system", None)]
    assert await count(h, models.EventAttendance) == 2


async def test_people_reimport_is_idempotent(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    await members(h)
    first = await import_one(h, hackathon())
    second = await import_one(h, hackathon(summary="A different summary"))
    assert second["result"] == "updated"
    assert second["event_id"] == first["event_id"]
    assert await count(h, models.EventAttendance) == 2
    assert await count(h, models.Confirmation) == 2
    assert await count(h, models.EventSourcePerson) == 4


async def test_people_existing_attendance_states(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    going = await h.user(login="going-member")
    speaker = await h.user(login="speaker-member")
    cancelled = await h.user(login="cancelled-member")
    created = await import_one(h, hackathon(people=[]))
    event_id = created["event_id"]
    await add_attendance(h, event_id, going, AttendanceStatus.GOING, AttendanceRole.ATTENDEE)
    await add_attendance(h, event_id, speaker, AttendanceStatus.ATTENDED, AttendanceRole.SPEAKER)
    await add_attendance(
        h, event_id, cancelled, AttendanceStatus.CANCELLED, AttendanceRole.ATTENDEE
    )
    people = [
        {"github_login": user.github_login, "display_name": user.display_name, "role": "attendee"}
        for user in (going, speaker, cancelled)
    ]
    result = await import_one(h, hackathon(people=people))
    assert result["result"] == "unchanged"
    going_row = await attendance_of(h, event_id, going)
    assert going_row.status == "attended"
    assert going_row.status_at == h.now
    assert going_row.trust == "trusted"
    speaker_row = await attendance_of(h, event_id, speaker)
    assert speaker_row.role == "speaker"
    assert len(await system_confirmations(h, speaker_row.id)) == 1
    cancelled_row = await attendance_of(h, event_id, cancelled)
    assert cancelled_row.status == "cancelled"
    assert cancelled_row.trust == "self"
    assert await system_confirmations(h, cancelled_row.id) == []


async def test_people_banned_member_not_matched(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    await h.user(login="mira-tidal", banned_at=h.now)
    result = await import_one(h, hackathon(people=[winner("mira-tidal", 1)]))
    async with h.db() as session:
        rows = list(
            await session.scalars(
                select(models.EventSourcePerson).where(
                    models.EventSourcePerson.event_id == UUID(result["event_id"])
                )
            )
        )
    assert len(rows) == 1
    assert rows[0].matched_user_id is None
    assert await count(h, models.EventAttendance) == 0
    assert await count(h, models.Confirmation) == 0


async def test_people_winner_verification(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    created = await import_one(h, hackathon(people=[]))
    event_id = created["event_id"]
    solo = await h.user(login="solo-winner")
    pair = [await h.user(login="pair-one"), await h.user(login="pair-two")]
    unplaced = await h.user(login="unplaced-winner")
    rejected = await h.user(login="rejected-winner")
    solo_team = await add_team(h, event_id, [solo], "unverified")
    pair_team = await add_team(h, event_id, pair, "unverified")
    unplaced_team = await add_team(h, event_id, [unplaced], "unverified")
    rejected_team = await add_team(h, event_id, [rejected], "rejected")
    people = [
        winner("solo-winner", 2),
        winner("pair-one", 3),
        winner("pair-two", 1),
        winner("unplaced-winner", None),
        winner("rejected-winner", 1),
    ]
    await import_one(h, hackathon(people=people))
    solo_result = await result_of(h, solo_team)
    assert solo_result.place == 2
    assert solo_result.place_status == "verified"
    assert solo_result.place_verified_at == h.now
    assert solo_result.place_verified_by is None
    pair_result = await result_of(h, pair_team)
    assert pair_result.place == 1
    assert pair_result.place_status == "verified"
    unplaced_result = await result_of(h, unplaced_team)
    assert unplaced_result.place is None
    assert unplaced_result.place_status == "unverified"
    rejected_result = await result_of(h, rejected_team)
    assert rejected_result.place is None
    assert rejected_result.place_status == "rejected"
    assert rejected_result.place_verified_at is None


async def test_people_winner_list_arrives_later(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    created = await import_one(h, hackathon(people=[]))
    event_id = created["event_id"]
    member = await h.user(login="late-winner")
    team_id = await add_team(h, event_id, [member], None)
    people = [winner("late-winner", 1)]
    await import_one(h, hackathon(people=people))
    assert await count(h, models.ProjectResult) == 0
    async with h.db() as session:
        await add_result(session, team_id, "unverified")
    h.advance(timedelta(hours=1))
    organizer = {"display_name": "Leo Ashford", "role": "organizer"}
    later = hackathon(people=[*people, organizer])
    await import_one(h, later)
    verified_at = h.now
    result = await result_of(h, team_id)
    assert result.place == 1
    assert result.place_status == "verified"
    assert result.place_verified_at == verified_at
    h.advance(timedelta(hours=1))
    again = await import_one(h, later)
    assert again["result"] == "unchanged"
    assert (await result_of(h, team_id)).place_verified_at == verified_at


async def test_people_draft_and_cancelled_events(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    member_a, _ = await members(h)
    draft = await import_one(h, hackathon(confidence=0.5))
    assert draft["result"] == "draft"
    attendance = await attendance_of(h, draft["event_id"], member_a)
    assert attendance.status == "attended"
    assert len(await system_confirmations(h, attendance.id)) == 1
    source = {**HACKATHON["source"], "source_id": "forge:other"}
    created = await import_one(h, hackathon(source=source, title="Other build", people=[]))
    assert created["result"] == "created"
    cancelled = await import_one(
        h, hackathon(source=source, title="Other build", status="cancelled")
    )
    assert cancelled["result"] == "updated"
    assert (
        await count(
            h, models.EventAttendance, models.EventAttendance.event_id == UUID(created["event_id"])
        )
        == 0
    )
