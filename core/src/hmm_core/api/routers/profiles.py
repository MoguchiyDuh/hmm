from fastapi import APIRouter

from hmm_core.api.errors import PROBLEM_RESPONSES

router = APIRouter(tags=["profiles"], responses=PROBLEM_RESPONSES)
internal = APIRouter(tags=["profiles"], responses=PROBLEM_RESPONSES)
