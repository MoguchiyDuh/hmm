from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.responses import RedirectResponse

from hmm_core.api.errors import PROBLEM_RESPONSES, not_implemented
from hmm_core.api.schemas.identity import CurrentUser, TelegramLinkCode
from hmm_core.api.security import session_user

router = APIRouter(tags=["identity"], responses=PROBLEM_RESPONSES)
internal = APIRouter(tags=["identity"], responses=PROBLEM_RESPONSES)


@router.get(
    "/auth/github/login",
    status_code=status.HTTP_302_FOUND,
    response_class=RedirectResponse,
    summary="Sign in with GitHub",
)
async def login(next: Annotated[str | None, Query()] = None) -> Response:
    not_implemented()


@router.get(
    "/auth/github/callback",
    status_code=status.HTTP_302_FOUND,
    response_class=RedirectResponse,
    summary="Complete GitHub sign-in",
)
async def github_callback(
    code: Annotated[str, Query()], state: Annotated[str, Query()]
) -> Response:
    not_implemented()


@router.post(
    "/auth/logout", status_code=status.HTTP_204_NO_CONTENT, summary="Delete current session"
)
async def logout(actor: Annotated[None, Depends(session_user)]) -> None:
    not_implemented()


@router.post(
    "/auth/logout-all", status_code=status.HTTP_204_NO_CONTENT, summary="Delete all own sessions"
)
async def logout_all(actor: Annotated[None, Depends(session_user)]) -> None:
    not_implemented()


@router.get("/me", status_code=status.HTTP_200_OK, summary="Get current user")
async def get_current_user(actor: Annotated[None, Depends(session_user)]) -> CurrentUser:
    not_implemented()


@router.post(
    "/me/telegram/link-code",
    status_code=status.HTTP_201_CREATED,
    summary="Create Telegram link code",
)
async def create_telegram_link_code(
    actor: Annotated[None, Depends(session_user)],
) -> TelegramLinkCode:
    not_implemented()


@router.delete("/me/telegram", status_code=status.HTTP_204_NO_CONTENT, summary="Unlink Telegram")
async def unlink_telegram(actor: Annotated[None, Depends(session_user)]) -> None:
    not_implemented()
