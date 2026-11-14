from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, Response, status
from hmm_contracts.common import (
    AreaKey,
    EventFormat,
    EventId,
    EventKind,
    EventOrigin,
    EventPhase,
    FormationPhase,
)
from hmm_contracts.imports import (
    EventImportBatch,
    EventImportResponse,
    ParserHeartbeat,
    TrackedEventsResponse,
)
from pydantic import AwareDatetime

from hmm_core.catalog import service as catalog
from hmm_core.catalog.schemas import (
    CreateEventBody,
    EventDetail,
    EventFieldLock,
    EventRevision,
    EventSummaryPage,
    MergeEventBody,
    UpdateEventBody,
)
from hmm_core.platform.clock import CurrentClock
from hmm_core.platform.db import Session
from hmm_core.platform.errors import PROBLEM_RESPONSES, not_implemented
from hmm_core.platform.pagination import CursorParams
from hmm_core.platform.schemas import ReasonBody
from hmm_core.platform.security import AdminAuth, MemberAuth, ParserAuth
from hmm_core.projects.schemas import Project

router = APIRouter(tags=["catalog"], responses=PROBLEM_RESPONSES)
internal = APIRouter(tags=["catalog"], responses=PROBLEM_RESPONSES)


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
        Literal[FormationPhase.OPEN, FormationPhase.REVIEW, FormationPhase.LOCKED] | None,
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
    return await catalog.list_events(
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
    event = await catalog.visible_event(session, id, actor)
    return await catalog.event_detail(session, event, actor, clock.now())


@router.get("/events/{id}/projects", status_code=status.HTTP_200_OK, summary="List event projects")
async def list_event_projects(id: EventId, actor: MemberAuth) -> list[Project]:
    not_implemented()


@router.post("/admin/events", status_code=status.HTTP_201_CREATED, summary="Create a manual event")
async def create_event(
    body: CreateEventBody,
    actor: AdminAuth,
    session: Session,
    request: Request,
    clock: CurrentClock,
) -> EventDetail:
    event = await catalog.create_manual_event(
        session, request.app.state.jobs, body, actor, clock.now()
    )
    return await catalog.event_detail(session, event, actor, clock.now())


@router.get(
    "/admin/events/review-queue",
    status_code=status.HTTP_200_OK,
    summary="List events awaiting review",
)
async def list_review_queue(actor: AdminAuth) -> list[EventDetail]:
    not_implemented()


@router.patch(
    "/admin/events/{id}", status_code=status.HTTP_200_OK, summary="Edit and lock event fields"
)
async def update_event(id: EventId, body: UpdateEventBody, actor: AdminAuth) -> EventDetail:
    not_implemented()


@router.get(
    "/admin/events/{id}/locks", status_code=status.HTTP_200_OK, summary="List event field locks"
)
async def list_event_locks(id: EventId, actor: AdminAuth) -> list[EventFieldLock]:
    not_implemented()


@router.delete(
    "/admin/events/{id}/locks/{field}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unlock an event field",
)
async def unlock_event_field(
    id: EventId,
    field: str,
    actor: AdminAuth,
    reason: Annotated[str | None, Query()] = None,
) -> Response:
    not_implemented()


@router.get(
    "/admin/events/{id}/revisions", status_code=status.HTTP_200_OK, summary="List event revisions"
)
async def list_event_revisions(id: EventId, actor: AdminAuth) -> list[EventRevision]:
    not_implemented()


@router.post(
    "/admin/events/{id}/publish", status_code=status.HTTP_200_OK, summary="Publish an event"
)
async def publish_event(
    id: EventId, actor: AdminAuth, body: ReasonBody | None = None
) -> EventDetail:
    not_implemented()


@router.post(
    "/admin/events/{id}/reject", status_code=status.HTTP_200_OK, summary="Reject an event draft"
)
async def reject_event(
    id: EventId, actor: AdminAuth, body: ReasonBody | None = None
) -> EventDetail:
    not_implemented()


@router.post(
    "/admin/events/{id}/merge", status_code=status.HTTP_200_OK, summary="Merge an event draft"
)
async def merge_event(id: EventId, body: MergeEventBody, actor: AdminAuth) -> EventDetail:
    not_implemented()


@router.post("/admin/events/{id}/cancel", status_code=status.HTTP_200_OK, summary="Cancel an event")
async def cancel_event(id: EventId, body: ReasonBody, actor: AdminAuth) -> EventDetail:
    not_implemented()


@internal.post("/events/import", status_code=status.HTTP_200_OK, summary="Import an event batch")
async def import_events(body: EventImportBatch, actor: ParserAuth) -> EventImportResponse:
    not_implemented()


@internal.get(
    "/events/tracked", status_code=status.HTTP_200_OK, summary="List tracked parser events"
)
async def list_tracked_events(
    actor: ParserAuth,
    pagination: Annotated[CursorParams, Depends()],
) -> TrackedEventsResponse:
    not_implemented()


@internal.post(
    "/parser/heartbeat", status_code=status.HTTP_204_NO_CONTENT, summary="Record a parser heartbeat"
)
async def record_parser_heartbeat(body: ParserHeartbeat, actor: ParserAuth) -> Response:
    not_implemented()
