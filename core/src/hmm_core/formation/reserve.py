from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from hmm_contracts.algo import (
    EnrolledMember,
    RankedMember,
    ReserveRequest,
    TeamComposition,
    TeamMemberSnapshot,
)
from hmm_contracts.common import (
    EnrollmentStatus,
    EventStatus,
    FormationPhase,
    MemberRole,
    MemberState,
)
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.formation.common import CAPACITY_TEAM_STATES, LIVE_ENROLLMENT_STATES
from hmm_core.formation.leads import formation_sizes
from hmm_core.platform.settings import load_settings
from hmm_core.scores.snapshot import build_member_snapshots

RESERVE_RANK_REASON = "Ranked by the reserve planner"


def validated_reserve_ranking(
    reserve_ids: Sequence[UUID], ranked: Sequence[RankedMember]
) -> list[RankedMember]:
    allowed = set(reserve_ids)
    kept: list[RankedMember] = []
    users: set[UUID] = set()
    ranks: set[int] = set()
    for entry in ranked:
        if entry.user_id not in allowed or entry.user_id in users or entry.rank in ranks:
            continue
        kept.append(entry)
        users.add(entry.user_id)
        ranks.add(entry.rank)
    result = [
        RankedMember(user_id=entry.user_id, rank=index, reason=entry.reason or RESERVE_RANK_REASON)
        for index, entry in enumerate(sorted(kept, key=lambda entry: entry.rank), start=1)
    ]
    for user_id in reserve_ids:
        if user_id in users:
            continue
        users.add(user_id)
        result.append(
            RankedMember(user_id=user_id, rank=len(result) + 1, reason=RESERVE_RANK_REASON)
        )
    return result


async def build_reserve_request(
    session: AsyncSession,
    event: models.Event,
    formation: models.EventFormation,
    now: datetime,
) -> ReserveRequest | None:
    from hmm_core.catalog import service as catalog

    settings = await load_settings(session)
    sizes = formation_sizes(event, formation, settings)
    reserve_rows = list(
        await session.scalars(
            select(models.Enrollment)
            .join(models.User, models.User.id == models.Enrollment.user_id)
            .where(
                models.Enrollment.event_id == event.id,
                models.Enrollment.status == EnrollmentStatus.RESERVE.value,
                models.User.banned_at.is_(None),
            )
            .order_by(models.Enrollment.created_at, models.Enrollment.id)
        )
    )
    if not reserve_rows:
        return None
    projects_rows = list(
        await session.scalars(
            select(models.Project)
            .where(
                models.Project.event_id == event.id,
                models.Project.team_status.in_(
                    sorted(status.value for status in CAPACITY_TEAM_STATES)
                ),
            )
            .order_by(models.Project.created_at, models.Project.id)
        )
    )
    project_ids = [project.id for project in projects_rows]
    member_rows: list[models.ProjectMember] = (
        list(
            await session.scalars(
                select(models.ProjectMember)
                .where(
                    models.ProjectMember.project_id.in_(project_ids),
                    models.ProjectMember.left_at.is_(None),
                    models.ProjectMember.is_team_row.is_(True),
                )
                .order_by(models.ProjectMember.joined_at, models.ProjectMember.id)
            )
        )
        if project_ids
        else []
    )
    member_ids = {row.user_id for row in member_rows}
    enrollments: dict[UUID, models.Enrollment] = (
        {
            enrollment.user_id: enrollment
            for enrollment in await session.scalars(
                select(models.Enrollment).where(
                    models.Enrollment.event_id == event.id,
                    models.Enrollment.user_id.in_(member_ids),
                    models.Enrollment.status.in_(
                        sorted(status.value for status in LIVE_ENROLLMENT_STATES)
                    ),
                )
            )
        }
        if member_ids
        else {}
    )
    area_ids = {row.area_id for row in member_rows if row.area_id is not None} | {
        row.preferred_area_id for row in reserve_rows if row.preferred_area_id is not None
    }
    areas: dict[UUID, str] = (
        {
            area_id: key
            for area_id, key in await session.execute(
                select(models.Area.id, models.Area.key).where(models.Area.id.in_(area_ids))
            )
        }
        if area_ids
        else {}
    )
    snapshots = await build_member_snapshots(
        session, sorted(member_ids | {row.user_id for row in reserve_rows}), now
    )
    teams: list[TeamComposition] = []
    for project in projects_rows:
        members: list[TeamMemberSnapshot] = []
        for row in member_rows:
            if row.project_id != project.id or row.user_id not in snapshots:
                continue
            enrollment = enrollments.get(row.user_id)
            members.append(
                TeamMemberSnapshot(
                    **snapshots[row.user_id].model_dump(),
                    role=MemberRole(row.role),
                    area=areas.get(row.area_id) if row.area_id is not None else None,
                    state=MemberState(row.state),
                    willing_to_lead=enrollment.willing_to_lead if enrollment is not None else False,
                    claimed_lead=row.user_id == project.lead_user_id,
                    enrolled_at=enrollment.created_at if enrollment is not None else row.joined_at,
                )
            )
        teams.append(
            TeamComposition(
                project_id=project.id,
                members=members,
                open_seats=max(0, sizes.target - len(members)),
            )
        )
    reserve = [
        EnrolledMember(
            **snapshots[row.user_id].model_dump(),
            preferred_area=areas.get(row.preferred_area_id)
            if row.preferred_area_id is not None
            else None,
            willing_to_lead=row.willing_to_lead,
            enrolled_at=row.created_at,
            note=row.note or "",
        )
        for row in reserve_rows
        if row.user_id in snapshots
    ]
    return ReserveRequest(
        hackathon=await catalog.event_snapshot(session, event.id), teams=teams, reserve=reserve
    )


async def apply_reserve_ranking(
    session: AsyncSession,
    event_id: UUID,
    ranked: Sequence[RankedMember] | None,
) -> None:
    rows = list(
        await session.scalars(
            select(models.Enrollment)
            .where(
                models.Enrollment.event_id == event_id,
                models.Enrollment.status == EnrollmentStatus.RESERVE.value,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if not rows:
        return None
    if ranked is None:
        next_rank = max(
            (row.reserve_rank for row in rows if row.reserve_rank is not None), default=0
        )
        for row in sorted(
            (row for row in rows if row.reserve_rank is None),
            key=lambda row: (row.created_at, str(row.id)),
        ):
            next_rank += 1
            row.reserve_rank = next_rank
    else:
        final = validated_reserve_ranking([row.user_id for row in rows], ranked)
        ranks = {entry.user_id: entry.rank for entry in final}
        for row in rows:
            rank = ranks.get(row.user_id)
            if rank is not None:
                row.reserve_rank = rank
    await session.flush()
    return None


async def reserve_ranking_events(session: AsyncSession) -> list[UUID]:
    return list(
        await session.scalars(
            select(models.Event.id)
            .join(models.EventFormation, models.EventFormation.event_id == models.Event.id)
            .where(
                models.Event.formation_enabled.is_(True),
                models.Event.status == EventStatus.PUBLISHED.value,
                models.EventFormation.phase.in_(
                    [FormationPhase.REVIEW.value, FormationPhase.LOCKED.value]
                ),
                exists().where(
                    models.Enrollment.event_id == models.Event.id,
                    models.Enrollment.status == EnrollmentStatus.RESERVE.value,
                ),
            )
            .order_by(models.Event.id)
        )
    )
