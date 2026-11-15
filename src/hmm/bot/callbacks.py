from enum import StrEnum
from uuid import UUID

from aiogram.filters.callback_data import CallbackData

from hmm.common import ConfirmationFactType, MentorshipOutcome
from hmm.notifications.payloads import ActionKey


class EventOp(StrEnum):
    SHOW = "show"
    GOING = "going"
    ENROLL = "enroll"
    WITHDRAW = "withdraw"
    PARTY = "party"


class ActionCallback(CallbackData, prefix="a"):
    key: ActionKey
    entity_id: UUID


class EventCallback(CallbackData, prefix="e"):
    op: EventOp
    event_id: UUID


class HelpCallback(CallbackData, prefix="h"):
    request_id: UUID


class CheckinCallback(CallbackData, prefix="ci"):
    mentorship_id: UUID


class CloseCallback(CallbackData, prefix="cl"):
    mentorship_id: UUID


class OutcomeCallback(CallbackData, prefix="o"):
    outcome: MentorshipOutcome
    mentorship_id: UUID


class ConfirmCallback(CallbackData, prefix="c"):
    fact_type: ConfirmationFactType
    fact_id: UUID
