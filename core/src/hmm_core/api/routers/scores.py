from fastapi import APIRouter

from hmm_core.api.errors import PROBLEM_RESPONSES

router = APIRouter(tags=["scores"], responses=PROBLEM_RESPONSES)
internal = APIRouter(tags=["scores"], responses=PROBLEM_RESPONSES)
