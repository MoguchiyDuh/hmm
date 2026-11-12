from enum import StrEnum

from hmm_contracts.notifications import AdminAlert, Notification
from pydantic import Field

from hmm_core.api.schemas.common import (
    ApiModel,
    Note,
    ReasonBody,
    WithAllowedActions,
)


class NotificationAction(StrEnum):
    READ = "read"


class AlertAction(StrEnum):
    RESOLVE = "resolve"


class InboxNotification(Notification, WithAllowedActions[NotificationAction]):
    pass


class Alert(AdminAlert, WithAllowedActions[AlertAction]):
    pass


class UnreadCount(ApiModel):
    count: int = Field(ge=0)


class ResolveAlertBody(ReasonBody):
    note: Note
