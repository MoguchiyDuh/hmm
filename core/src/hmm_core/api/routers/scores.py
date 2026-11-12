from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from hmm_contracts.common import (
    ActivityEntryId,
    AreaKey,
    FlagId,
    FlagStatus,
    FlagSubjectType,
    Page,
    RestrictionId,
    UserId,
)

from hmm_core.api.errors import PROBLEM_RESPONSES, not_implemented
from hmm_core.api.pagination import CursorParams
from hmm_core.api.schemas.common import Reason
from hmm_core.api.schemas.scores import (
    ActivityEntry,
    AreaScore,
    AreaScoreEvent,
    CreateActivityBody,
    CreateXpAdjustmentBody,
    Flag,
    LiftRestrictionBody,
    MemberMetrics,
    ResolveFlagBody,
    Restriction,
    SetScoreOverrideBody,
    UpdateActivityBody,
    XpAdjustment,
)
from hmm_core.api.security import admin, member

router = APIRouter(tags=["scores"], responses=PROBLEM_RESPONSES)
internal = APIRouter(tags=["scores"], responses=PROBLEM_RESPONSES)


@router.get(
    "/users/{id}/scores", status_code=status.HTTP_200_OK, summary="Get effective scores and reasons"
)
async def get_scores(id: UserId, actor: Annotated[None, Depends(member)]) -> list[AreaScore]:
    not_implemented()


@router.get(
    "/users/{id}/scores/history",
    status_code=status.HTTP_200_OK,
    summary="Get score history for self or admin",
)
async def get_score_history(
    id: UserId,
    actor: Annotated[None, Depends(member)],
    pagination: Annotated[CursorParams, Depends()],
    area: Annotated[AreaKey | None, Query()] = None,
) -> Page[AreaScoreEvent]:
    not_implemented()


@router.get("/users/{id}/activity", status_code=status.HTTP_200_OK, summary="List solo activity")
async def list_activity(
    id: UserId,
    actor: Annotated[None, Depends(member)],
    pagination: Annotated[CursorParams, Depends()],
) -> Page[ActivityEntry]:
    not_implemented()


@router.post("/me/activity", status_code=status.HTTP_201_CREATED, summary="Create solo activity")
async def create_activity(
    body: CreateActivityBody, actor: Annotated[None, Depends(member)]
) -> ActivityEntry:
    not_implemented()


@router.patch("/me/activity/{id}", status_code=status.HTTP_200_OK, summary="Edit own solo activity")
async def update_activity(
    id: ActivityEntryId, body: UpdateActivityBody, actor: Annotated[None, Depends(member)]
) -> ActivityEntry:
    not_implemented()


@router.delete(
    "/me/activity/{id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete own solo activity"
)
async def delete_activity(id: ActivityEntryId, actor: Annotated[None, Depends(member)]) -> None:
    not_implemented()


@router.get("/users/{id}/metrics", status_code=status.HTTP_200_OK, summary="Get member metrics")
async def get_metrics(id: UserId, actor: Annotated[None, Depends(member)]) -> MemberMetrics:
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
    actor: Annotated[None, Depends(admin)],
) -> AreaScore:
    not_implemented()


@router.delete(
    "/admin/users/{id}/scores/{area_key}/override",
    status_code=status.HTTP_200_OK,
    summary="Remove area score override",
)
async def remove_score_override(
    id: UserId,
    area_key: AreaKey,
    actor: Annotated[None, Depends(admin)],
    body: Reason | None = None,
) -> AreaScore:
    not_implemented()


@router.post(
    "/admin/users/{id}/xp-adjustments",
    status_code=status.HTTP_201_CREATED,
    summary="Adjust member XP",
)
async def create_xp_adjustment(
    id: UserId, body: CreateXpAdjustmentBody, actor: Annotated[None, Depends(admin)]
) -> XpAdjustment:
    not_implemented()


@router.get(
    "/admin/flags",
    status_code=status.HTTP_200_OK,
    summary="List flagged subjects and related members",
)
async def list_flags(
    actor: Annotated[None, Depends(admin)],
    pagination: Annotated[CursorParams, Depends()],
    status: Annotated[FlagStatus, Query()] = FlagStatus.TO_CHECK,
    user: Annotated[UserId | None, Query()] = None,
    subject_type: Annotated[FlagSubjectType | None, Query()] = None,
) -> Page[Flag]:
    not_implemented()


@router.post(
    "/admin/flags/{id}/resolve",
    status_code=status.HTTP_200_OK,
    summary="Resolve suspicious activity",
)
async def resolve_flag(
    id: FlagId, body: ResolveFlagBody, actor: Annotated[None, Depends(admin)]
) -> Flag:
    not_implemented()


@router.get(
    "/admin/restrictions", status_code=status.HTTP_200_OK, summary="List member restrictions"
)
async def list_restrictions(
    actor: Annotated[None, Depends(admin)],
    user: Annotated[UserId | None, Query()] = None,
    active: Annotated[bool | None, Query()] = None,
) -> list[Restriction]:
    not_implemented()


@router.delete(
    "/admin/restrictions/{id}", status_code=status.HTTP_204_NO_CONTENT, summary="Lift restriction"
)
async def lift_restriction(
    id: RestrictionId, body: LiftRestrictionBody, actor: Annotated[None, Depends(admin)]
) -> None:
    not_implemented()


@router.post(
    "/admin/users/{id}/recompute",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Enqueue member recomputation",
)
async def recompute_member(
    id: UserId, actor: Annotated[None, Depends(admin)], body: Reason | None = None
) -> None:
    not_implemented()
