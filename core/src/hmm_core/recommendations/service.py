import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

import procrastinate
from hmm_contracts.algo import (
    EnrolledMember,
    MemberSnapshot,
    Rec,
    Recommendations,
    RecommendRequest,
    TeamMemberSnapshot,
)
from hmm_contracts.algo import ProjectSnapshot as AlgoProjectSnapshot
from hmm_contracts.algo import Recruiting as AlgoRecruiting
from hmm_contracts.common import (
    EnrollmentStatus,
    ErrorCode,
    InviteStatus,
    JoinRequestStatus,
    MemberRole,
    MemberState,
    ProjectStatus,
    RecommendationStatus,
    RecommendationSubjectType,
    TeamStatus,
)
from pydantic import ValidationError
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.catalog import service as catalog
from hmm_core.formation import service as formation
from hmm_core.identity.service import ONBOARDED, dnd_active
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.jobs import JobName, defer_job
from hmm_core.platform.schemas import UserRef
from hmm_core.recommendations import schemas
from hmm_core.scores.snapshot import build_member_snapshots

RECOMMEND_LIMIT_ENROLLED = 100
RECOMMEND_LIMIT_OTHERS = 100
REFRESH_INTERVAL = timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class RecommendationContext:
    project: models.Project
    request: RecommendRequest
    enrolled_ids: frozenset[UUID]
    others_ids: frozenset[UUID]
    input_hash: str


def recommendation_refreshable(project: models.Project) -> bool:
    if project.team_status is not None:
        return project.team_status in {state.value for state in formation.CAPACITY_TEAM_STATES}
    return project.recruiting is not None


def hash_recommendations(request: RecommendRequest, algo_version: str) -> str:
    payload = {"algo_version": algo_version, "request": request.model_dump(mode="json")}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def recommendation_project(session: AsyncSession, project_id: UUID) -> models.Project:
    project = await session.get(models.Project, project_id)
    if project is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return project


def eligible_user(user: models.User, now: datetime) -> bool:
    return (
        user.onboarding_status in ONBOARDED and user.banned_at is None and not dnd_active(user, now)
    )


async def area_keys_by_id(session: AsyncSession, ids: Sequence[UUID]) -> dict[UUID, str]:
    if not ids:
        return {}
    rows = await session.execute(
        select(models.Area.id, models.Area.key).where(models.Area.id.in_(ids))
    )
    return {area_id: key for area_id, key in rows}


async def live_enrollment_map(
    session: AsyncSession, event_id: UUID, user_ids: Sequence[UUID]
) -> dict[UUID, models.Enrollment]:
    if not user_ids:
        return {}
    rows = await session.scalars(
        select(models.Enrollment)
        .where(
            models.Enrollment.event_id == event_id,
            models.Enrollment.user_id.in_(user_ids),
            models.Enrollment.status.in_(
                sorted(state.value for state in formation.LIVE_ENROLLMENT_STATES)
            ),
        )
        .order_by(models.Enrollment.status_at.desc(), models.Enrollment.id.desc())
    )
    enrollments: dict[UUID, models.Enrollment] = {}
    for row in rows:
        enrollments.setdefault(row.user_id, row)
    return enrollments


async def event_team_member_ids(session: AsyncSession, event_id: UUID) -> set[UUID]:
    return set(
        await session.scalars(
            select(models.ProjectMember.user_id).where(
                models.ProjectMember.event_id == event_id,
                models.ProjectMember.left_at.is_(None),
                models.ProjectMember.is_team_row.is_(True),
            )
        )
    )


async def other_members(
    session: AsyncSession, excluded: set[UUID], now: datetime
) -> list[MemberSnapshot]:
    users = await session.scalars(select(models.User).order_by(models.User.id))
    ids = [user.id for user in users if eligible_user(user, now) and user.id not in excluded][
        :RECOMMEND_LIMIT_OTHERS
    ]
    snapshots = await build_member_snapshots(session, ids, now)
    return [snapshots[user_id] for user_id in ids if user_id in snapshots]


async def team_candidates(
    session: AsyncSession,
    project: models.Project,
    now: datetime,
    project_member_ids: set[UUID],
    team_ids: set[UUID],
    invited_ids: set[UUID],
) -> tuple[list[TeamMemberSnapshot], list[EnrolledMember], list[MemberSnapshot]]:
    event_id = project.event_id
    if event_id is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if await session.get(models.EventFormation, event_id) is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    team_rows = list(
        await session.scalars(
            select(models.ProjectMember)
            .where(
                models.ProjectMember.project_id == project.id,
                models.ProjectMember.left_at.is_(None),
            )
            .order_by(
                models.ProjectMember.role != MemberRole.LEAD,
                models.ProjectMember.joined_at,
                models.ProjectMember.id,
            )
        )
    )
    member_ids = [row.user_id for row in team_rows]
    snapshots = await build_member_snapshots(session, member_ids, now)
    enrollments = await live_enrollment_map(session, event_id, member_ids)
    member_areas = await area_keys_by_id(
        session, [row.area_id for row in team_rows if row.area_id is not None]
    )
    team = [
        TeamMemberSnapshot(
            **snapshots[row.user_id].model_dump(),
            role=MemberRole(row.role),
            area=member_areas.get(row.area_id) if row.area_id is not None else None,
            state=MemberState(row.state),
            willing_to_lead=bool(enrollments[row.user_id].willing_to_lead)
            if row.user_id in enrollments
            else False,
            claimed_lead=row.user_id == project.lead_user_id,
            enrolled_at=enrollments[row.user_id].created_at
            if row.user_id in enrollments
            else row.joined_at,
        )
        for row in team_rows
        if row.user_id in snapshots
    ]
    forming = project.team_status == TeamStatus.FORMING.value
    status = EnrollmentStatus.ACTIVE if forming else EnrollmentStatus.RESERVE
    order = (
        (models.Enrollment.created_at, models.Enrollment.id)
        if forming
        else (
            models.Enrollment.reserve_rank.asc().nulls_last(),
            models.Enrollment.status_at,
            models.Enrollment.id,
        )
    )
    rows = await session.execute(
        select(models.Enrollment, models.User)
        .join(models.User, models.User.id == models.Enrollment.user_id)
        .where(models.Enrollment.event_id == event_id, models.Enrollment.status == status.value)
        .order_by(*order)
    )
    kept = [
        enrollment
        for enrollment, user in rows
        if eligible_user(user, now)
        and enrollment.user_id not in team_ids
        and enrollment.user_id not in project_member_ids
    ][:RECOMMEND_LIMIT_ENROLLED]
    candidates = await build_member_snapshots(session, [row.user_id for row in kept], now)
    areas = await area_keys_by_id(
        session, [row.preferred_area_id for row in kept if row.preferred_area_id is not None]
    )
    enrolled = [
        EnrolledMember(
            **candidates[enrollment.user_id].model_dump(),
            preferred_area=areas.get(enrollment.preferred_area_id)
            if enrollment.preferred_area_id is not None
            else None,
            willing_to_lead=enrollment.willing_to_lead,
            enrolled_at=enrollment.created_at,
            note=enrollment.note or "",
        )
        for enrollment in kept
        if enrollment.user_id in candidates
    ]
    excluded = project_member_ids | team_ids | {row.user_id for row in kept} | invited_ids
    others = await other_members(session, excluded, now)
    return team, enrolled, others


async def standalone_candidates(
    session: AsyncSession,
    project: models.Project,
    now: datetime,
    project_member_ids: set[UUID],
    team_ids: set[UUID],
    invited_ids: set[UUID],
) -> tuple[list[EnrolledMember], list[MemberSnapshot]]:
    rows = list(
        await session.execute(
            select(models.JoinRequest, models.User)
            .join(models.User, models.User.id == models.JoinRequest.user_id)
            .where(
                models.JoinRequest.project_id == project.id,
                models.JoinRequest.status == JoinRequestStatus.PENDING.value,
            )
            .order_by(models.JoinRequest.created_at, models.JoinRequest.id)
        )
    )
    requester_ids = {join_request.user_id for join_request, _ in rows}
    blocked = team_ids | project_member_ids
    kept = [
        join_request
        for join_request, user in rows
        if eligible_user(user, now) and join_request.user_id not in blocked
    ][:RECOMMEND_LIMIT_ENROLLED]
    kept_ids = [row.user_id for row in kept]
    willing = (
        await live_enrollment_map(session, project.event_id, kept_ids)
        if project.event_id is not None
        else {}
    )
    candidates = await build_member_snapshots(session, kept_ids, now)
    enrolled = [
        EnrolledMember(
            **candidates[join_request.user_id].model_dump(),
            preferred_area=None,
            willing_to_lead=bool(willing[join_request.user_id].willing_to_lead)
            if join_request.user_id in willing
            else False,
            enrolled_at=join_request.created_at,
            note=join_request.note or "",
        )
        for join_request in kept
        if join_request.user_id in candidates
    ]
    excluded = project_member_ids | team_ids | requester_ids | invited_ids | set(kept_ids)
    others = await other_members(session, excluded, now)
    return enrolled, others


async def recommendation_context(
    session: AsyncSession, project: models.Project, now: datetime, algo_version: str
) -> RecommendationContext:
    area_keys = list(
        await session.scalars(
            select(models.Area.key)
            .join(models.ProjectArea, models.ProjectArea.area_id == models.Area.id)
            .where(models.ProjectArea.project_id == project.id)
            .order_by(models.Area.sort, models.Area.key)
        )
    )
    links = project.links if isinstance(project.links, list) else []
    project_snapshot = AlgoProjectSnapshot(
        id=project.id,
        name=project.name,
        goal=project.goal,
        description_md=project.description_md,
        areas=area_keys,
        recruiting=AlgoRecruiting.model_validate(project.recruiting)
        if project.recruiting is not None
        else None,
        event_id=project.event_id,
        status=ProjectStatus(project.status),
        team_status=TeamStatus(project.team_status) if project.team_status is not None else None,
        links=[str(link) for link in links],
    )
    event_snapshot = (
        await catalog.event_snapshot(session, project.event_id)
        if project.event_id is not None
        else None
    )
    project_member_ids = set(
        await session.scalars(
            select(models.ProjectMember.user_id).where(
                models.ProjectMember.project_id == project.id,
                models.ProjectMember.left_at.is_(None),
            )
        )
    )
    invited_ids = set(
        await session.scalars(
            select(models.Invite.user_id).where(
                models.Invite.project_id == project.id,
                models.Invite.status == InviteStatus.PENDING.value,
            )
        )
    )
    team_ids = (
        await event_team_member_ids(session, project.event_id)
        if project.event_id is not None
        else set()
    )
    if project.team_status is not None:
        if event_snapshot is None:
            raise ProblemError(ErrorCode.NOT_FOUND)
        team, enrolled, others = await team_candidates(
            session, project, now, project_member_ids, team_ids, invited_ids
        )
    else:
        team = []
        enrolled, others = await standalone_candidates(
            session, project, now, project_member_ids, team_ids, invited_ids
        )
    request = RecommendRequest(
        event=event_snapshot,
        project=project_snapshot,
        team=team,
        enrolled=enrolled,
        others=others,
        limit_enrolled=RECOMMEND_LIMIT_ENROLLED,
        limit_others=RECOMMEND_LIMIT_OTHERS,
    )
    return RecommendationContext(
        project=project,
        request=request,
        enrolled_ids=frozenset(member.user_id for member in enrolled),
        others_ids=frozenset(member.user_id for member in others),
        input_hash=hash_recommendations(request, algo_version),
    )


async def latest_row(session: AsyncSession, project_id: UUID) -> models.RecommendationSet | None:
    return await session.scalar(
        select(models.RecommendationSet)
        .where(
            models.RecommendationSet.subject_type == RecommendationSubjectType.PROJECT.value,
            models.RecommendationSet.subject_id == project_id,
        )
        .order_by(models.RecommendationSet.created_at.desc(), models.RecommendationSet.id.desc())
        .limit(1)
    )


async def latest_ready_row(
    session: AsyncSession, project_id: UUID
) -> models.RecommendationSet | None:
    return await session.scalar(
        select(models.RecommendationSet)
        .where(
            models.RecommendationSet.subject_type == RecommendationSubjectType.PROJECT.value,
            models.RecommendationSet.subject_id == project_id,
            models.RecommendationSet.status == RecommendationStatus.READY.value,
        )
        .order_by(models.RecommendationSet.created_at.desc(), models.RecommendationSet.id.desc())
        .limit(1)
    )


def stored_recs(items: object) -> list[Rec]:
    if not isinstance(items, list):
        return []
    return [Rec.model_validate(item) for item in items]


async def project_recommendations(
    session: AsyncSession, project_id: UUID, actor: models.User, now: datetime
) -> schemas.ProjectRecommendations:
    project = await recommendation_project(session, project_id)
    if actor.id != project.lead_user_id and not actor.is_admin:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if not recommendation_refreshable(project):
        raise ProblemError(ErrorCode.INVALID_STATE, "Quick-pick is not available for this project")
    row = await latest_row(session, project.id)
    served = await latest_ready_row(session, project.id)
    status = RecommendationStatus(row.status) if row is not None else RecommendationStatus.PENDING
    stale = (
        served is None
        or (await recommendation_context(session, project, now, served.algo_version)).input_hash
        != served.input_hash
    )
    computed_at = served.completed_at if served is not None else None
    enrolled_recs = stored_recs(served.enrolled) if served is not None else []
    others_recs = stored_recs(served.others) if served is not None else []
    user_ids = {rec.user_id for rec in (*enrolled_recs, *others_recs)}
    users = (
        {
            user.id: user
            for user in await session.scalars(
                select(models.User).where(models.User.id.in_(user_ids))
            )
        }
        if user_ids
        else {}
    )

    def build(recs: list[Rec]) -> list[schemas.Recommendation]:
        return [
            schemas.Recommendation(
                **rec.model_dump(),
                profile=UserRef(
                    id=users[rec.user_id].id,
                    display_name=users[rec.user_id].display_name,
                    avatar_url=users[rec.user_id].avatar_url,
                ),
            )
            for rec in recs
            if rec.user_id in users
        ]

    return schemas.ProjectRecommendations(
        status=status,
        stale=stale,
        computed_at=computed_at,
        enrolled=build(enrolled_recs),
        others=build(others_recs),
        allowed_actions=[schemas.RecommendationsAction.REFRESH]
        if actor.id == project.lead_user_id
        else [],
    )


@dataclass(frozen=True, slots=True)
class PreparedRefresh:
    row_id: UUID
    request: RecommendRequest
    enrolled_ids: frozenset[UUID]
    others_ids: frozenset[UUID]


async def prepare_refresh(
    session: AsyncSession, project: models.Project, now: datetime, algo_version: str
) -> PreparedRefresh | None:
    if not recommendation_refreshable(project):
        return None
    context = await recommendation_context(session, project, now, algo_version)
    previous = await latest_ready_row(session, project.id)
    if previous is not None and previous.input_hash == context.input_hash:
        return None
    row = models.RecommendationSet(
        subject_type=RecommendationSubjectType.PROJECT.value,
        subject_id=project.id,
        input_hash=context.input_hash,
        status=RecommendationStatus.PENDING.value,
        error=None,
        enrolled=None,
        others=None,
        algo_version=algo_version,
        completed_at=None,
    )
    session.add(row)
    await session.flush()
    return PreparedRefresh(
        row_id=row.id,
        request=context.request,
        enrolled_ids=context.enrolled_ids,
        others_ids=context.others_ids,
    )


async def locked_row(session: AsyncSession, row_id: UUID) -> models.RecommendationSet | None:
    return await session.scalar(
        select(models.RecommendationSet)
        .where(models.RecommendationSet.id == row_id)
        .with_for_update()
    )


async def mark_ready(
    session: AsyncSession, row_id: UUID, result: Recommendations, now: datetime
) -> None:
    row = await locked_row(session, row_id)
    if row is None:
        return None
    row.status = RecommendationStatus.READY.value
    row.enrolled = [rec.model_dump(mode="json") for rec in result.enrolled]
    row.others = [rec.model_dump(mode="json") for rec in result.others]
    row.error = None
    row.completed_at = now


async def mark_failed(session: AsyncSession, row_id: UUID, error: Exception, now: datetime) -> None:
    row = await locked_row(session, row_id)
    if row is None:
        return None
    row.status = RecommendationStatus.FAILED.value
    row.error = str(error)[:1000]
    row.completed_at = now


def validated_group(items: Sequence[Rec], ids: frozenset[UUID]) -> tuple[list[Rec], int]:
    kept: list[Rec] = []
    seen: set[UUID] = set()
    dropped = 0
    for item in items:
        if item.user_id not in ids or item.user_id in seen:
            dropped += 1
            continue
        reasons = [
            reason.strip() for reason in item.reasons if isinstance(reason, str) and reason.strip()
        ][:3]
        if not reasons:
            dropped += 1
            continue
        try:
            rec = Rec(
                user_id=item.user_id,
                score=min(1.0, max(0.0, float(item.score))),
                reasons=reasons,
                tags=[str(tag) for tag in item.tags],
                suggested_area=item.suggested_area,
            )
        except ValidationError:
            dropped += 1
            continue
        seen.add(item.user_id)
        kept.append(rec)
    return kept, dropped


def validated_recommendations(
    result: Recommendations, enrolled_ids: frozenset[UUID], others_ids: frozenset[UUID]
) -> tuple[Recommendations, int]:
    enrolled, dropped_enrolled = validated_group(result.enrolled, enrolled_ids)
    others, dropped_others = validated_group(result.others, others_ids)
    return Recommendations(enrolled=enrolled, others=others), dropped_enrolled + dropped_others


async def enqueue_project_refresh(
    session: AsyncSession, jobs: procrastinate.App, project_id: UUID
) -> None:
    await defer_job(session, jobs, JobName.REFRESH_RECOMMENDATIONS, project_id=str(project_id))


async def enqueue_event_refresh(
    session: AsyncSession, jobs: procrastinate.App, event_id: UUID
) -> None:
    project_ids = await session.scalars(
        select(models.Project.id)
        .where(
            models.Project.event_id == event_id,
            or_(
                models.Project.team_status.in_(
                    sorted(state.value for state in formation.CAPACITY_TEAM_STATES)
                ),
                and_(
                    models.Project.team_status.is_(None),
                    models.Project.recruiting.is_not(None),
                    func.jsonb_typeof(models.Project.recruiting) != "null",
                ),
            ),
        )
        .order_by(models.Project.id)
    )
    for project_id in project_ids.all():
        await enqueue_project_refresh(session, jobs, project_id)


async def request_refresh(
    session: AsyncSession,
    jobs: procrastinate.App,
    project_id: UUID,
    actor: models.User,
    now: datetime,
) -> None:
    project = await session.scalar(
        select(models.Project)
        .where(models.Project.id == project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if project is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if actor.id != project.lead_user_id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if not recommendation_refreshable(project):
        raise ProblemError(ErrorCode.INVALID_STATE, "Quick-pick is not available for this project")
    if (
        project.recommendation_refresh_at is not None
        and now - project.recommendation_refresh_at < REFRESH_INTERVAL
    ):
        raise ProblemError(
            ErrorCode.RATE_LIMITED, "Recommendations can be refreshed once per minute"
        )
    project.recommendation_refresh_at = now
    await session.flush()
    await enqueue_project_refresh(session, jobs, project.id)
