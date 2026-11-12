from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from hmm_contracts.algo import GithubSummary, MentorOffer, RatingSummary, ReviewIn
from hmm_contracts.common import CvTextStatus, GithubSnapshotStatus, Locale, ReviewContext
from hmm_contracts.notifications import NotificationType
from pydantic import AwareDatetime, Field, HttpUrl, field_validator

from hmm_core.api.schemas.common import ApiInput, AreaScoreSummary, UserRef, WithAllowedActions


class UpdateProfileBody(ApiInput):
    display_name: str | None = None
    about_md: str | None = Field(default=None, max_length=5000)
    contacts: dict[str, str] | None = Field(default=None, max_length=10)
    timezone: str | None = None
    locale: Locale | None = None

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError("Unknown IANA timezone") from exc
        return value


class UpdateAvailabilityBody(ApiInput):
    dnd_until: AwareDatetime | Literal["indefinite"] | None


class Availability(WithAllowedActions):
    dnd_until: AwareDatetime | Literal["indefinite"] | None


class UserSummary(UserRef, WithAllowedActions):
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
    timezone: str | None
    locale: Locale
    github: GithubSummary | None
    github_status: GithubSnapshotStatus | None
    scores_reviewed_at: AwareDatetime | None
    reviews: list[ReviewIn]
    created_at: AwareDatetime
    updated_at: AwareDatetime


class Cv(WithAllowedActions):
    filename: str
    url: HttpUrl
    text_status: CvTextStatus


class UploadCvBody(ApiInput):
    file: bytes = Field(
        max_length=5_000_000,
        description="PDF, UTF-8 Markdown (.md), or UTF-8 Typst (.typ); maximum 5 MB",
        json_schema_extra={"format": "binary"},
    )


class CvUpload(WithAllowedActions):
    filename: str
    text_status: CvTextStatus


class NotificationSetting(ApiInput):
    type: NotificationType
    web: bool
    telegram: bool


class UpdateNotificationSettingsBody(ApiInput):
    items: list[NotificationSetting]


class NotificationSettings(WithAllowedActions):
    items: list[NotificationSetting]
