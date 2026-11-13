from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from hmm_contracts.common import (
    ActivityEntryId,
    AreaKey,
    FlagId,
    FlagStatus,
    FlagSubjectType,
    RestrictionId,
    UserId,
)

from hmm_core.platform.errors import PROBLEM_RESPONSES, not_implemented
from hmm_core.platform.pagination import CursorParams
from hmm_core.platform.schemas import ReasonBody
from hmm_core.platform.security import AdminAuth, MemberAuth
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

router = APIRouter(tags=["scores"], responses=PROBLEM_RESPONSES)
internal = APIRouter(tags=["scores"], responses=PROBLEM_RESPONSES)


@router.get(
    "/users/{id}/scores", status_code=status.HTTP_200_OK, summary="Get effective scores and reasons"
)
async def get_scores(id: UserId, actor: MemberAuth) -> list[MemberAreaScore]:
    not_implemented()


@router.get(
    "/users/{id}/scores/history",
    status_code=status.HTTP_200_OK,
    summary="Get score history for self or admin",
)
async def get_score_history(
    id: UserId,
    actor: MemberAuth,
    pagination: Annotated[CursorParams, Depends()],
    area: Annotated[AreaKey | None, Query()] = None,
) -> AreaScoreEventPage:
    not_implemented()


@router.get("/users/{id}/activity", status_code=status.HTTP_200_OK, summary="List solo activity")
async def list_activity(
    id: UserId,
    actor: MemberAuth,
    pagination: Annotated[CursorParams, Depends()],
) -> ActivityEntryPage:
    not_implemented()


@router.post("/me/activity", status_code=status.HTTP_201_CREATED, summary="Create solo activity")
async def create_activity(body: CreateActivityBody, actor: MemberAuth) -> ActivityEntry:
    not_implemented()


@router.patch("/me/activity/{id}", status_code=status.HTTP_200_OK, summary="Edit own solo activity")
async def update_activity(
    id: ActivityEntryId, body: UpdateActivityBody, actor: MemberAuth
) -> ActivityEntry:
    not_implemented()


@router.delete(
    "/me/activity/{id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete own solo activity"
)
async def delete_activity(id: ActivityEntryId, actor: MemberAuth) -> None:
    not_implemented()


@router.get("/users/{id}/metrics", status_code=status.HTTP_200_OK, summary="Get member metrics")
async def get_metrics(id: UserId, actor: MemberAuth) -> MemberMetrics:
    not_implemented()


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
) -> MemberAreaScore:
    not_implemented()


@router.delete(
    "/admin/users/{id}/scores/{area_key}/override",
    status_code=status.HTTP_200_OK,
    summary="Remove area score override",
)
async def remove_score_override(
    id: UserId,
    area_key: AreaKey,
    actor: AdminAuth,
    body: ReasonBody | None = None,
) -> MemberAreaScore:
    not_implemented()


@router.post(
    "/admin/users/{id}/xp-adjustments",
    status_code=status.HTTP_201_CREATED,
    summary="Adjust member XP",
)
async def create_xp_adjustment(
    id: UserId, body: CreateXpAdjustmentBody, actor: AdminAuth
) -> XpAdjustment:
    not_implemented()


@router.get(
    "/admin/flags",
    status_code=status.HTTP_200_OK,
    summary="List flagged subjects and related members",
)
async def list_flags(
    actor: AdminAuth,
    pagination: Annotated[CursorParams, Depends()],
    status: Annotated[FlagStatus, Query()] = FlagStatus.TO_CHECK,
    user: Annotated[UserId | None, Query()] = None,
    subject_type: Annotated[FlagSubjectType | None, Query()] = None,
) -> FlagPage:
    not_implemented()


@router.post(
    "/admin/flags/{id}/resolve",
    status_code=status.HTTP_200_OK,
    summary="Resolve suspicious activity",
)
async def resolve_flag(id: FlagId, body: ResolveFlagBody, actor: AdminAuth) -> Flag:
    not_implemented()


@router.get(
    "/admin/restrictions", status_code=status.HTTP_200_OK, summary="List member restrictions"
)
async def list_restrictions(
    actor: AdminAuth,
    user: Annotated[UserId | None, Query()] = None,
    active: Annotated[bool | None, Query()] = None,
) -> list[Restriction]:
    not_implemented()


@router.delete(
    "/admin/restrictions/{id}", status_code=status.HTTP_204_NO_CONTENT, summary="Lift restriction"
)
async def lift_restriction(id: RestrictionId, body: LiftRestrictionBody, actor: AdminAuth) -> None:
    not_implemented()


@router.post(
    "/admin/users/{id}/recompute",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Enqueue member recomputation",
)
async def recompute_member(id: UserId, actor: AdminAuth, body: ReasonBody | None = None) -> None:
    not_implemented()
