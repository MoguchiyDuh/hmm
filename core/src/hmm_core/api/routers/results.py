from fastapi import APIRouter

from hmm_core.api.errors import PROBLEM_RESPONSES

router = APIRouter(tags=["results"], responses=PROBLEM_RESPONSES)
internal = APIRouter(tags=["results"], responses=PROBLEM_RESPONSES)
