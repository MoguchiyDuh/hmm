from fastapi import APIRouter

from hmm_core.api.errors import PROBLEM_RESPONSES

router = APIRouter(tags=["identity"], responses=PROBLEM_RESPONSES)
internal = APIRouter(tags=["identity"], responses=PROBLEM_RESPONSES)
