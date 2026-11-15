from enum import StrEnum

from pydantic import Field

from hmm.common import Page
from hmm.notifications.payloads import AdminAlert, Notification
from hmm.platform.schemas import (
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


class AlertPage(Page[Alert]):
    pass


class InboxNotificationPage(Page[InboxNotification]):
    pass
