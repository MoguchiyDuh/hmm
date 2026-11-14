from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from hmm_contracts.common import (
    ActivityEntryId,
    ActorKind,
    AreaKey,
    AuditAction,
    AuditEntityType,
    ErrorCode,
    FlagId,
    FlagStatus,
    FlagSubjectType,
    RestrictionId,
    UserId,
)

from hmm_core import models
from hmm_core.platform.audit import record_audit
from hmm_core.platform.clock import CurrentClock
from hmm_core.platform.db import Session
from hmm_core.platform.errors import PROBLEM_RESPONSES, ProblemError
from hmm_core.platform.jobs import JobName, defer_job
from hmm_core.platform.pagination import CursorParams
from hmm_core.platform.schemas import ReasonBody
from hmm_core.platform.security import AdminAuth, MemberAuth
from hmm_core.scores import moderation, restrictions, service
from hmm_core.scores.schemas import (
    ActivityEntry,
    ActivityEntryPage,
    AreaScoreEventPage,
    CreateActivityBody,
    CreateXpAdjustmentBody,
    Flag,
    FlagPage,
    LiftRestrictionBody,
    MemberAreaScore,
    MemberMetrics,
    ResolveFlagBody,
    Restriction,
    SetScoreOverrideBody,
    UpdateActivityBody,
    XpAdjustment,
)
from hmm_core.scores.snapshot import build_member_snapshot

router = APIRouter(tags=["scores"], responses=PROBLEM_RESPONSES)
internal = APIRouter(tags=["scores"], responses=PROBLEM_RESPONSES)


@router.get(
    "/users/{id}/scores", status_code=status.HTTP_200_OK, summary="Get effective scores and reasons"
)
async def get_scores(id: UserId, actor: MemberAuth, session: Session) -> list[MemberAreaScore]:
    return await service.list_scores(session, id, actor)


@router.get(
    "/users/{id}/scores/history",
    status_code=status.HTTP_200_OK,
    summary="Get score history for self or admin",
)
async def get_score_history(
    id: UserId,
    actor: MemberAuth,
    session: Session,
    pagination: Annotated[CursorParams, Depends()],
    area: Annotated[AreaKey | None, Query()] = None,
) -> AreaScoreEventPage:
    return await service.list_score_history(session, id, actor, pagination, area)


@router.get("/users/{id}/activity", status_code=status.HTTP_200_OK, summary="List solo activity")
async def list_activity(
    id: UserId,
    actor: MemberAuth,
    session: Session,
    pagination: Annotated[CursorParams, Depends()],
) -> ActivityEntryPage:
    return await service.list_activity(session, id, actor, pagination)


@router.post("/me/activity", status_code=status.HTTP_201_CREATED, summary="Create solo activity")
async def create_activity(
    body: CreateActivityBody,
    actor: MemberAuth,
    session: Session,
    clock: CurrentClock,
    request: Request,
) -> ActivityEntry:
    return await service.create_activity(session, request.app.state.jobs, actor, body, clock.now())


@router.patch("/me/activity/{id}", status_code=status.HTTP_200_OK, summary="Edit own solo activity")
async def update_activity(
    id: ActivityEntryId,
    body: UpdateActivityBody,
    actor: MemberAuth,
    session: Session,
    clock: CurrentClock,
    request: Request,
) -> ActivityEntry:
    return await service.update_activity(
        session, request.app.state.jobs, actor, id, body, clock.now()
    )


@router.delete(
    "/me/activity/{id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete own solo activity"
)
async def delete_activity(
    id: ActivityEntryId,
    actor: MemberAuth,
    session: Session,
    clock: CurrentClock,
    request: Request,
) -> None:
    await service.delete_activity(session, request.app.state.jobs, actor, id, clock.now())


@router.get("/users/{id}/metrics", status_code=status.HTTP_200_OK, summary="Get member metrics")
async def get_metrics(
    id: UserId, actor: MemberAuth, session: Session, clock: CurrentClock
) -> MemberMetrics:
    snapshot = await build_member_snapshot(session, id, clock.now())
    return MemberMetrics(
        xp=snapshot.xp,
        activity=snapshot.activity,
        reputation=snapshot.reputation,
        hackathons_finished=snapshot.hackathons_finished,
        projects_shipped=snapshot.projects_shipped,
        mentorships_completed=snapshot.mentorships_completed,
    )


@router.put(
    "/admin/users/{id}/scores/{area_key}/override",
    status_code=status.HTTP_200_OK,
    summary="Set area score override",
)
async def set_score_override(
    id: UserId,
    area_key: AreaKey,
    body: SetScoreOverrideBody,
    actor: AdminAuth,
    session: Session,
    clock: CurrentClock,
    request: Request,
) -> MemberAreaScore:
    view = await service.set_score_override(
        session,
        request.app.state.jobs,
        id,
        area_key,
        body.value,
        body.reason,
        body.evidence,
        actor.id,
        clock.now(),
    )
    return service.member_area_score(view.area, view.score, actor=actor)


@router.delete(
    "/admin/users/{id}/scores/{area_key}/override",
    status_code=status.HTTP_200_OK,
    summary="Remove area score override",
)
async def remove_score_override(
    id: UserId,
    area_key: AreaKey,
    actor: AdminAuth,
    session: Session,
    clock: CurrentClock,
    request: Request,
    body: ReasonBody | None = None,
) -> MemberAreaScore:
    view = await service.remove_score_override(
        session,
        request.app.state.jobs,
        id,
        area_key,
        body.reason if body is not None else None,
        actor.id,
        clock.now(),
    )
    return service.member_area_score(view.area, view.score, actor=actor)


@router.post(
    "/admin/users/{id}/xp-adjustments",
    status_code=status.HTTP_201_CREATED,
    summary="Adjust member XP",
)
async def create_xp_adjustment(
    id: UserId,
    body: CreateXpAdjustmentBody,
    actor: AdminAuth,
    session: Session,
    clock: CurrentClock,
) -> XpAdjustment:
    row = await moderation.create_xp_adjustment(session, id, body, actor.id, clock.now())
    return XpAdjustment.model_validate(row)


@router.get(
    "/admin/flags",
    status_code=status.HTTP_200_OK,
    summary="List flagged subjects and related members",
)
async def list_flags(
    actor: AdminAuth,
    session: Session,
    pagination: Annotated[CursorParams, Depends()],
    status: Annotated[FlagStatus, Query()] = FlagStatus.TO_CHECK,
    user: Annotated[UserId | None, Query()] = None,
    subject_type: Annotated[FlagSubjectType | None, Query()] = None,
) -> FlagPage:
    return await moderation.list_flags(
        session, pagination, status=status, user_id=user, subject_type=subject_type
    )


@router.post(
    "/admin/flags/{id}/resolve",
    status_code=status.HTTP_200_OK,
    summary="Resolve suspicious activity",
)
async def resolve_flag(
    id: FlagId,
    body: ResolveFlagBody,
    actor: AdminAuth,
    session: Session,
    request: Request,
    clock: CurrentClock,
) -> Flag:
    row = await moderation.resolve_flag(
        session, request.app.state.jobs, id, body, actor.id, clock.now()
    )
    return (await moderation.flag_views(session, [row]))[0]


@router.get(
    "/admin/restrictions", status_code=status.HTTP_200_OK, summary="List member restrictions"
)
async def list_restrictions(
    actor: AdminAuth,
    session: Session,
    clock: CurrentClock,
    user: Annotated[UserId | None, Query()] = None,
    active: Annotated[bool | None, Query()] = None,
) -> list[Restriction]:
    rows = await restrictions.list_restrictions(
        session, user_id=user, active=active, now=clock.now()
    )
    return [restrictions.restriction_view(row, clock.now()) for row in rows]


@router.delete(
    "/admin/restrictions/{id}", status_code=status.HTTP_204_NO_CONTENT, summary="Lift restriction"
)
async def lift_restriction(
    id: RestrictionId,
    body: LiftRestrictionBody,
    actor: AdminAuth,
    session: Session,
    request: Request,
    clock: CurrentClock,
) -> None:
    await restrictions.lift_restriction(
        session, request.app.state.jobs, id, actor.id, body.reason, clock.now()
    )


@router.post(
    "/admin/users/{id}/recompute",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Enqueue member recomputation",
)
async def recompute_member(
    id: UserId,
    actor: AdminAuth,
    session: Session,
    request: Request,
    body: ReasonBody | None = None,
) -> None:
    if await session.get(models.User, id) is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    await record_audit(
        session,
        actor=actor.id,
        actor_kind=ActorKind.ADMIN,
        action=AuditAction.MEMBER_RECOMPUTE,
        entity_type=AuditEntityType.USER,
        entity_id=str(id),
        reason=body.reason if body is not None else None,
    )
    await defer_job(session, request.app.state.jobs, JobName.RECOMPUTE_MEMBER, user_id=str(id))
