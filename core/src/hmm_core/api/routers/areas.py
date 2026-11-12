from typing import Annotated

from fastapi import APIRouter, Depends, status

from hmm_core.api.errors import PROBLEM_RESPONSES, not_implemented
from hmm_core.api.schemas.areas import AreaResponse
from hmm_core.api.security import member

router = APIRouter(tags=["areas"], responses=PROBLEM_RESPONSES)
internal = APIRouter(tags=["areas"], responses=PROBLEM_RESPONSES)


@router.get("/areas", status_code=status.HTTP_200_OK, summary="List areas")
async def list_areas(actor: Annotated[None, Depends(member)]) -> list[AreaResponse]:
    not_implemented()
