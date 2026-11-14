from enum import StrEnum
from typing import Literal

from hmm_contracts.algo import GithubSummary, MentorOffer, RatingSummary, ReviewIn
from hmm_contracts.common import (
    CvTextStatus,
    GithubSnapshotStatus,
    Locale,
    Page,
    ReviewContext,
    TimeZoneName,
)
from hmm_contracts.notifications import NotificationType
from pydantic import AwareDatetime, Field, HttpUrl

from hmm_core.platform.schemas import (
    ApiModel,
    AreaScoreSummary,
    Markdown,
    Name,
    Title,
    UserRef,
    WithAllowedActions,
)


class AvailabilityAction(StrEnum):
    EDIT = "edit"


class UserAction(StrEnum):
    REQUEST_HELP = "request_help"


class CvAction(StrEnum):
    REPLACE = "replace"
    DELETE = "delete"


class NotificationSettingsAction(StrEnum):
    EDIT = "edit"


class UpdateProfileBody(ApiModel):
    display_name: Name | None = None
    about_md: Markdown | None = None
    contacts: dict[Name, Title] | None = Field(default=None, max_length=10)
    timezone: TimeZoneName | None = None
    locale: Locale | None = None


class UpdateAvailabilityBody(ApiModel):
    dnd_until: AwareDatetime | Literal["indefinite"] | None


class Availability(WithAllowedActions[AvailabilityAction]):
    dnd_until: AwareDatetime | Literal["indefinite"] | None


class UserSummary(UserRef, WithAllowedActions[UserAction]):
    github_login: str
    github_url: HttpUrl
    area_scores: list[AreaScoreSummary]
    xp: float
    activity: float = Field(ge=0)
    reputation: dict[ReviewContext, RatingSummary]
    dnd_until: AwareDatetime | Literal["indefinite"] | None
    mentor_offers: list[MentorOffer]


class UserProfile(UserSummary):
    about_md: str
    contacts: dict[str, str]
    timezone: TimeZoneName | None
    locale: Locale
    github: GithubSummary | None
    github_status: GithubSnapshotStatus | None
    scores_reviewed_at: AwareDatetime | None
    reviews: list[ReviewIn]
    created_at: AwareDatetime
    updated_at: AwareDatetime


class Cv(WithAllowedActions[CvAction]):
    filename: str
    url: HttpUrl
    text_status: CvTextStatus | None
    text_error: str | None


class UploadCvBody(ApiModel):
    file: bytes = Field(
        ...,
        description="PDF, UTF-8 Markdown (.md), or UTF-8 Typst (.typ)",
        json_schema_extra={"format": "binary"},
    )


class CvUpload(WithAllowedActions[CvAction]):
    filename: str
    text_status: CvTextStatus


class NotificationSetting(ApiModel):
    type: NotificationType
    web: bool
    telegram: bool


class UpdateNotificationSettingsBody(ApiModel):
    items: list[NotificationSetting]


class NotificationSettings(WithAllowedActions[NotificationSettingsAction]):
    items: list[NotificationSetting]


class UserSummaryPage(Page[UserSummary]):
    pass
