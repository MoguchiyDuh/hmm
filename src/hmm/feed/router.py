from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from hmm.common import AreaKey, ConfirmationId, FactType, UserId
from hmm.feed import confirmations as feed_confirmations
from hmm.feed import history as feed_history
from hmm.feed import listing
from hmm.feed.schemas import (
    Confirmation,
    CreateConfirmationBody,
    FactPage,
    FeedItemPage,
    FeedKind,
    PendingConfirmationGroup,
)
from hmm.platform.clock import CurrentClock
from hmm.platform.db import Session
from hmm.platform.errors import PROBLEM_RESPONSES
from hmm.platform.jobs import Jobs
from hmm.platform.pagination import CursorParams
from hmm.platform.security import MemberAuth
from hmm.platform.settings import load_settings

router = APIRouter(tags=["feed"], responses=PROBLEM_RESPONSES)


@router.get("/feed", status_code=status.HTTP_200_OK, summary="List community feed")
async def list_feed(
    actor: MemberAuth,
    pagination: Annotated[CursorParams, Depends()],
    session: Session,
    clock: CurrentClock,
    kind: Annotated[FeedKind | None, Query()] = None,
    area: Annotated[AreaKey | None, Query()] = None,
    mine_areas: Annotated[bool, Query()] = False,
) -> FeedItemPage:
    return await listing.list_feed(
        session, actor, pagination, kind=kind, area=area, mine_areas=mine_areas, now=clock.now()
    )


@router.get(
    "/me/confirmations/pending",
    status_code=status.HTTP_200_OK,
    summary="List confirmable facts grouped by event or project",
)
async def list_pending_confirmations(
    actor: MemberAuth,
    session: Session,
    clock: CurrentClock,
) -> list[PendingConfirmationGroup]:
    return await feed_history.list_pending_confirmations(session, actor, clock.now())


@router.post("/confirmations", status_code=status.HTTP_201_CREATED, summary="Confirm a fact")
async def create_confirmation(
    body: CreateConfirmationBody,
    actor: MemberAuth,
    session: Session,
    clock: CurrentClock,
    jobs: Jobs,
) -> Confirmation:
    now = clock.now()
    target = await feed_confirmations.confirmation_target(
        session, actor.id, body.fact_type, body.fact_id, now
    )
    row = await feed_confirmations.add_confirmation(
        session,
        jobs,
        fact_type=body.fact_type,
        fact_id=body.fact_id,
        subject_user_id=target.subject_user_id,
        confirmer_id=actor.id,
        source=target.source,
        now=now,
    )
    days = (await load_settings(session)).confirmation_withdraw_days
    return feed_confirmations.confirmation_view(row, actor.id, now, days)


@router.delete(
    "/confirmations/{id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Withdraw own confirmation within seven days",
)
async def withdraw_confirmation(
    id: ConfirmationId,
    actor: MemberAuth,
    session: Session,
    clock: CurrentClock,
    jobs: Jobs,
) -> None:
    await feed_confirmations.withdraw_confirmation(session, jobs, id, actor.id, clock.now())


@router.get(
    "/users/{id}/facts",
    status_code=status.HTTP_200_OK,
    summary="List member history with trust badges and awards",
)
async def list_user_facts(
    id: UserId,
    actor: MemberAuth,
    pagination: Annotated[CursorParams, Depends()],
    session: Session,
    clock: CurrentClock,
    fact_type: Annotated[FactType | None, Query(alias="type")] = None,
) -> FactPage:
    return await feed_history.list_user_facts(
        session, actor, id, pagination, fact_type=fact_type, now=clock.now()
    )
