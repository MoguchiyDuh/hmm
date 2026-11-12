from typing import Literal

from hmm_contracts.common import Locale, OnboardingStatus
from pydantic import AwareDatetime, Field, HttpUrl

from hmm_core.api.schemas.common import ApiModel, UserRef, WithAllowedActions


class TelegramLink(ApiModel):
    linked: bool
    username: str | None
    linked_at: AwareDatetime | None
    broken_at: AwareDatetime | None


class CurrentUser(UserRef, WithAllowedActions):
    github_login: str
    github_url: HttpUrl
    is_admin: bool
    onboarding_status: OnboardingStatus
    dnd_until: AwareDatetime | Literal["indefinite"] | None
    locale: Locale
    telegram: TelegramLink
    unread_count: int = Field(ge=0)


class TelegramLinkCode(ApiModel):
    code: str = Field(pattern=r"^[A-Z2-7]{8}$")
    deep_link: HttpUrl
    expires_at: AwareDatetime
