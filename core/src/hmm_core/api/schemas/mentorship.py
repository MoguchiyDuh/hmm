from datetime import date
from enum import StrEnum

from hmm_contracts.common import (
    AreaKey,
    HelpOfferId,
    HelpOfferStatus,
    HelpRequestFormat,
    HelpRequestId,
    HelpRequestStatus,
    MentorshipCheckinId,
    MentorshipId,
    MentorshipKind,
    MentorshipOutcome,
    MentorshipStatus,
    Trust,
    UserId,
)
from pydantic import AwareDatetime, Field

from hmm_core.api.schemas.common import (
    ApiInput,
    ApiModel,
    Note,
    Title,
    UserRef,
    WithAllowedActions,
)


class MentorOffersAction(StrEnum):
    EDIT = "edit"


class HelpOfferAction(StrEnum):
    ACCEPT = "accept"
    WITHDRAW = "withdraw"


class HelpRequestAction(StrEnum):
    EDIT = "edit"
    CANCEL = "cancel"
    DECLINE = "decline"
    OPEN = "open"
    OFFER_HELP = "offer_help"


class MentorshipAction(StrEnum):
    ACKNOWLEDGE = "acknowledge"
    CHECK_IN = "check_in"
    PROPOSE_CLOSE = "propose_close"
    CLOSE = "close"
    CANCEL = "cancel"
    CONFIRM = "confirm"


class MentorOfferInput(ApiInput):
    area_key: AreaKey
    topics: Note
    active: bool


class ReplaceMentorOffersBody(ApiInput):
    capacity: int | None = Field(default=None, ge=0)
    offers: list[MentorOfferInput]


class MentorOffers(WithAllowedActions[MentorOffersAction]):
    capacity: int | None = Field(ge=0)
    offers: list[MentorOfferInput]


class CreateHelpRequestBody(ApiInput):
    area_key: AreaKey
    topic: Title
    goal: Note
    format: HelpRequestFormat
    target_mentor_id: UserId | None = None


class UpdateHelpRequestBody(ApiInput):
    topic: Title | None = None
    goal: Note | None = None
    format: HelpRequestFormat | None = None


class CreateHelpOfferBody(ApiInput):
    message: Note


class HelpOffer(WithAllowedActions[HelpOfferAction]):
    id: HelpOfferId
    request_id: HelpRequestId
    mentor_id: UserId
    mentor: UserRef
    message: str
    status: HelpOfferStatus
    mentor_score: float = Field(ge=0, le=10)
    mentor_rating: float | None = Field(ge=1, le=10)
    mentor_review_count: int = Field(ge=0)
    past_mentorship_count: int = Field(ge=0)
    kind: MentorshipKind
    created_at: AwareDatetime
    updated_at: AwareDatetime


class HelpRequest(WithAllowedActions[HelpRequestAction]):
    id: HelpRequestId
    learner_id: UserId
    learner: UserRef
    area_key: AreaKey
    topic: str = Field(max_length=200)
    goal: str = Field(max_length=1000)
    format: HelpRequestFormat
    target_mentor_id: UserId | None
    status: HelpRequestStatus
    expires_at: AwareDatetime
    offers: list[HelpOffer]
    created_at: AwareDatetime
    updated_at: AwareDatetime


class CreateMentorshipCheckinBody(ApiInput):
    note: Note
    occurred_on: date
    minutes: int | None = Field(default=None, ge=0)


class MentorshipCheckin(ApiModel):
    id: MentorshipCheckinId
    mentorship_id: MentorshipId
    author_id: UserId
    note: str
    occurred_on: date
    minutes: int | None = Field(ge=0)
    created_at: AwareDatetime


class CloseMentorshipBody(ApiInput):
    outcome: MentorshipOutcome
    summary: Note | None = None


class AcknowledgeMentorshipBody(ApiInput):
    endorsement: Note | None = None


class Mentorship(WithAllowedActions[MentorshipAction]):
    id: MentorshipId
    request_id: HelpRequestId
    mentor_id: UserId
    learner_id: UserId
    mentor: UserRef
    learner: UserRef
    area_key: AreaKey
    kind: MentorshipKind
    mentor_score_at_start: float = Field(ge=0, le=10)
    learner_score_at_start: float = Field(ge=0, le=10)
    status: MentorshipStatus
    started_at: AwareDatetime
    last_checkin_at: AwareDatetime | None
    close_proposed_at: AwareDatetime | None
    closed_at: AwareDatetime | None
    outcome: MentorshipOutcome | None
    summary: str | None
    mentor_acknowledged_at: AwareDatetime | None
    endorsement: str | None
    mentor_trust: Trust
    learner_trust: Trust
    checkins: list[MentorshipCheckin]
    created_at: AwareDatetime
    updated_at: AwareDatetime
