from fastapi import APIRouter, Request, status

from hmm.areas import service
from hmm.areas.schemas import Area, CreateAreaBody, UpdateAreaBody
from hmm.catalog.import_schema import AreaListItem, AreaListResponse
from hmm.common import AreaId
from hmm.platform.clock import CurrentClock
from hmm.platform.db import Session
from hmm.platform.errors import PROBLEM_RESPONSES
from hmm.platform.schemas import ReasonBody
from hmm.platform.security import AdminAuth, MemberAuth, ParserAuth

router = APIRouter(tags=["areas"], responses=PROBLEM_RESPONSES)
internal = APIRouter(tags=["areas"], responses=PROBLEM_RESPONSES)


@router.get("/areas", status_code=status.HTTP_200_OK, summary="List areas")
async def list_areas(actor: MemberAuth, session: Session) -> list[Area]:
    areas = await service.list_areas(session, include_archived=actor.is_admin)
    return [service.area_view(area, actor) for area in areas]


@router.post("/admin/areas", status_code=status.HTTP_201_CREATED, summary="Create area")
async def create_area(
    body: CreateAreaBody, actor: AdminAuth, session: Session, request: Request
) -> Area:
    area = await service.create_area(session, request.app.state.jobs, body, actor.id)
    return service.area_view(area, actor)


@router.patch("/admin/areas/{id}", status_code=status.HTTP_200_OK, summary="Edit area")
async def update_area(id: AreaId, body: UpdateAreaBody, actor: AdminAuth, session: Session) -> Area:
    area = await service.update_area(session, id, body, actor.id)
    return service.area_view(area, actor)


@router.post("/admin/areas/{id}/archive", status_code=status.HTTP_200_OK, summary="Archive area")
async def archive_area(
    id: AreaId,
    actor: AdminAuth,
    session: Session,
    clock: CurrentClock,
    body: ReasonBody | None = None,
) -> Area:
    area = await service.archive_area(
        session, id, actor.id, body.reason if body else None, clock.now()
    )
    return service.area_view(area, actor)


@router.post(
    "/admin/areas/{id}/unarchive", status_code=status.HTTP_200_OK, summary="Unarchive area"
)
async def unarchive_area(
    id: AreaId,
    actor: AdminAuth,
    session: Session,
    clock: CurrentClock,
    body: ReasonBody | None = None,
) -> Area:
    area = await service.unarchive_area(
        session, id, actor.id, body.reason if body else None, clock.now()
    )
    return service.area_view(area, actor)


@internal.get("/areas", status_code=status.HTTP_200_OK, summary="List importable areas")
async def list_internal_areas(actor: ParserAuth, session: Session) -> AreaListResponse:
    areas = await service.list_areas(session, include_archived=False)
    return AreaListResponse(
        items=[
            AreaListItem(
                id=area.id,
                key=area.key,
                name=area.name,
                description=area.description,
                sort=area.sort,
            )
            for area in areas
        ],
        next_cursor=None,
    )
