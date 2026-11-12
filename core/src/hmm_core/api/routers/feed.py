from fastapi import APIRouter

from hmm_core.api.errors import PROBLEM_RESPONSES

router = APIRouter(tags=["feed"], responses=PROBLEM_RESPONSES)
internal = APIRouter(tags=["feed"], responses=PROBLEM_RESPONSES)
