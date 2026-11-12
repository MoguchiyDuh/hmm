from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from hmm_contracts.common import AreaKey, ConfirmationId, FactType, Page, UserId

from hmm_core.api.errors import PROBLEM_RESPONSES, not_implemented
from hmm_core.api.pagination import CursorParams
from hmm_core.api.schemas.feed import (
    Confirmation,
    CreateConfirmationBody,
    Fact,
    FeedItem,
    FeedKind,
    PendingConfirmationGroup,
)
from hmm_core.api.security import member

router = APIRouter(tags=["feed"], responses=PROBLEM_RESPONSES)
internal = APIRouter(tags=["feed"], responses=PROBLEM_RESPONSES)


@router.get("/feed", status_code=status.HTTP_200_OK, summary="List community feed")
async def list_feed(
    actor: Annotated[None, Depends(member)],
    pagination: Annotated[CursorParams, Depends()],
    kind: Annotated[FeedKind | None, Query()] = None,
    area: Annotated[AreaKey | None, Query()] = None,
    mine_areas: Annotated[bool, Query()] = False,
) -> Page[FeedItem]:
    not_implemented()


@router.get(
    "/me/confirmations/pending",
    status_code=status.HTTP_200_OK,
    summary="List confirmable facts grouped by event or project",
)
async def list_pending_confirmations(
    actor: Annotated[None, Depends(member)],
) -> list[PendingConfirmationGroup]:
    not_implemented()


@router.post("/confirmations", status_code=status.HTTP_201_CREATED, summary="Confirm a fact")
async def create_confirmation(
    body: CreateConfirmationBody,
    actor: Annotated[None, Depends(member)],
) -> Confirmation:
    not_implemented()


@router.delete(
    "/confirmations/{id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Withdraw own confirmation within seven days",
)
async def withdraw_confirmation(
    id: ConfirmationId, actor: Annotated[None, Depends(member)]
) -> None:
    not_implemented()


@router.get(
    "/users/{id}/facts",
    status_code=status.HTTP_200_OK,
    summary="List member history with trust badges and awards",
)
async def list_user_facts(
    id: UserId,
    actor: Annotated[None, Depends(member)],
    pagination: Annotated[CursorParams, Depends()],
    fact_type: Annotated[FactType | None, Query(alias="type")] = None,
) -> Page[Fact]:
    not_implemented()
