from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import AwareDatetime

from hmm.catalog import changes, review
from hmm.catalog import events as catalog_events
from hmm.catalog import views as catalog_views
from hmm.catalog.schemas import (
    CreateEventBody,
    EventDetail,
    EventFieldLock,
    EventRevision,
    EventSummaryPage,
    MergeEventBody,
    UpdateEventBody,
)
from hmm.common import (
    ActorKind,
    AreaKey,
    ErrorCode,
    EventFormat,
    EventId,
    EventKind,
    EventOrigin,
    EventPhase,
    FormationPhase,
)
from hmm.platform.clock import CurrentClock
from hmm.platform.db import Session
from hmm.platform.errors import PROBLEM_RESPONSES, ProblemError
from hmm.platform.jobs import Jobs
from hmm.platform.pagination import CursorParams
from hmm.platform.schemas import ReasonBody
from hmm.platform.security import AdminAuth, MemberAuth
from hmm.projects.schemas import ProjectView

router = APIRouter(tags=["catalog"], responses=PROBLEM_RESPONSES)


@router.get("/events", status_code=status.HTTP_200_OK, summary="List events")
async def list_events(
    actor: MemberAuth,
    pagination: Annotated[CursorParams, Depends()],
    session: Session,
    clock: CurrentClock,
    kind: Annotated[EventKind | None, Query()] = None,
    phase: Annotated[EventPhase | None, Query()] = None,
    formation: Annotated[bool | None, Query()] = None,
    participation_phase: Annotated[
        FormationPhase | None,
        Query(),
    ] = None,
    area: Annotated[AreaKey | None, Query()] = None,
    format: Annotated[EventFormat | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
    from_: Annotated[AwareDatetime | None, Query(alias="from")] = None,
    to: Annotated[AwareDatetime | None, Query()] = None,
    mine: Annotated[bool | None, Query()] = None,
    origin: Annotated[EventOrigin | None, Query()] = None,
) -> EventSummaryPage:
    return await catalog_views.list_events(
        session,
        pagination,
        actor,
        clock.now(),
        kind=kind,
        phase=phase,
        formation=formation,
        participation_phase=participation_phase,
        area=area,
        format=format,
        q=q,
        from_=from_,
        to=to,
        mine=mine,
        origin=origin,
    )


@router.get("/events/{id}", status_code=status.HTTP_200_OK, summary="Get event details")
async def get_event(
    id: EventId, actor: MemberAuth, session: Session, clock: CurrentClock
) -> EventDetail:
    event = await catalog_views.visible_event(session, id, actor)
    return await catalog_views.event_detail(session, event, actor, clock.now())


@router.get("/events/{id}/projects", status_code=status.HTTP_200_OK, summary="List event projects")
async def list_event_projects(
    id: EventId, actor: MemberAuth, session: Session, clock: CurrentClock
) -> list[ProjectView]:
    return await catalog_views.list_event_projects(session, id, actor)


@router.post("/admin/events", status_code=status.HTTP_201_CREATED, summary="Create a manual event")
async def create_event(
    body: CreateEventBody,
    actor: AdminAuth,
    session: Session,
    jobs: Jobs,
    clock: CurrentClock,
) -> EventDetail:
    event = await catalog_events.create_manual_event(session, body, actor, clock.now())
    return await catalog_views.event_detail(session, event, actor, clock.now())


@router.get(
    "/admin/events/review-queue",
    status_code=status.HTTP_200_OK,
    summary="List events awaiting review",
)
async def list_review_queue(
    actor: AdminAuth, session: Session, clock: CurrentClock
) -> list[EventDetail]:
    return await review.review_queue(session, actor, clock.now())


@router.patch(
    "/admin/events/{id}", status_code=status.HTTP_200_OK, summary="Edit and lock event fields"
)
async def update_event(
    id: EventId,
    body: UpdateEventBody,
    actor: AdminAuth,
    session: Session,
    jobs: Jobs,
    clock: CurrentClock,
) -> EventDetail:
    event = await changes.update_event(session, jobs, id, body, actor, clock.now())
    return await catalog_views.event_detail(session, event, actor, clock.now())


@router.get(
    "/admin/events/{id}/locks", status_code=status.HTTP_200_OK, summary="List event field locks"
)
async def list_event_locks(id: EventId, actor: AdminAuth, session: Session) -> list[EventFieldLock]:
    return await changes.list_locks(session, id)


@router.delete(
    "/admin/events/{id}/locks/{field}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unlock an event field",
)
async def unlock_event_field(
    id: EventId,
    field: str,
    actor: AdminAuth,
    session: Session,
    clock: CurrentClock,
    reason: Annotated[str | None, Query()] = None,
) -> Response:
    await changes.unlock_field(session, id, field, actor, reason)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/admin/events/{id}/revisions", status_code=status.HTTP_200_OK, summary="List event revisions"
)
async def list_event_revisions(
    id: EventId, actor: AdminAuth, session: Session
) -> list[EventRevision]:
    return await changes.list_revisions(session, id)


@router.post(
    "/admin/events/{id}/publish", status_code=status.HTTP_200_OK, summary="Publish an event"
)
async def publish_event(
    id: EventId,
    actor: AdminAuth,
    session: Session,
    jobs: Jobs,
    clock: CurrentClock,
    body: ReasonBody | None = None,
) -> EventDetail:
    event = await review.publish_event(
        session, id, actor, body.reason if body else None, clock.now()
    )
    return await catalog_views.event_detail(session, event, actor, clock.now())


@router.post(
    "/admin/events/{id}/reject", status_code=status.HTTP_200_OK, summary="Reject an event draft"
)
async def reject_event(
    id: EventId,
    actor: AdminAuth,
    session: Session,
    jobs: Jobs,
    clock: CurrentClock,
    body: ReasonBody | None = None,
) -> EventDetail:
    event = await review.reject_event(session, id, actor, body.reason if body else None)
    return await catalog_views.event_detail(session, event, actor, clock.now())


@router.post(
    "/admin/events/{id}/merge", status_code=status.HTTP_200_OK, summary="Merge an event draft"
)
async def merge_event(
    id: EventId,
    body: MergeEventBody,
    actor: AdminAuth,
    session: Session,
    jobs: Jobs,
    clock: CurrentClock,
) -> EventDetail:
    event = await review.merge_event(session, jobs, id, body, actor, clock.now())
    return await catalog_views.event_detail(session, event, actor, clock.now())


@router.post("/admin/events/{id}/cancel", status_code=status.HTTP_200_OK, summary="Cancel an event")
async def cancel_event(
    id: EventId,
    body: ReasonBody,
    actor: AdminAuth,
    session: Session,
    jobs: Jobs,
    clock: CurrentClock,
) -> EventDetail:
    if body.reason is None:
        raise ProblemError(ErrorCode.VALIDATION_ERROR, "reason is required")
    event = await changes.cancel_event(
        session,
        id,
        reason=body.reason,
        actor=actor.id,
        actor_kind=ActorKind.ADMIN,
        now=clock.now(),
    )
    return await catalog_views.event_detail(session, event, actor, clock.now())
