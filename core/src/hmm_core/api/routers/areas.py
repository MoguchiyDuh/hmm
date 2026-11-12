from fastapi import APIRouter, status
from hmm_contracts.common import AreaId
from hmm_contracts.imports import AreaListResponse

from hmm_core.api.errors import PROBLEM_RESPONSES, not_implemented
from hmm_core.api.schemas.areas import Area, CreateAreaBody, UpdateAreaBody
from hmm_core.api.schemas.common import ReasonBody
from hmm_core.api.security import AdminAuth, MemberAuth, ParserAuth

router = APIRouter(tags=["areas"], responses=PROBLEM_RESPONSES)
internal = APIRouter(tags=["areas"], responses=PROBLEM_RESPONSES)


@router.get("/areas", status_code=status.HTTP_200_OK, summary="List areas")
async def list_areas(actor: MemberAuth) -> list[Area]:
    not_implemented()


@router.post("/admin/areas", status_code=status.HTTP_201_CREATED, summary="Create area")
async def create_area(body: CreateAreaBody, actor: AdminAuth) -> Area:
    not_implemented()


@router.patch("/admin/areas/{id}", status_code=status.HTTP_200_OK, summary="Edit area")
async def update_area(id: AreaId, body: UpdateAreaBody, actor: AdminAuth) -> Area:
    not_implemented()


@router.post("/admin/areas/{id}/archive", status_code=status.HTTP_200_OK, summary="Archive area")
async def archive_area(id: AreaId, actor: AdminAuth, body: ReasonBody | None = None) -> Area:
    not_implemented()


@router.post(
    "/admin/areas/{id}/unarchive", status_code=status.HTTP_200_OK, summary="Unarchive area"
)
async def unarchive_area(id: AreaId, actor: AdminAuth, body: ReasonBody | None = None) -> Area:
    not_implemented()


@internal.get("/areas", status_code=status.HTTP_200_OK, summary="List importable areas")
async def list_internal_areas(actor: ParserAuth) -> AreaListResponse:
    not_implemented()
