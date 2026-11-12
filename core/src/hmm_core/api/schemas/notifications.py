from hmm_contracts.notifications import AdminAlert, Notification
from pydantic import Field

from hmm_core.api.schemas.common import ApiModel, ReasonBody, WithAllowedActions


class InboxNotification(Notification, WithAllowedActions):
    pass


class Alert(AdminAlert, WithAllowedActions):
    pass


class UnreadCount(ApiModel):
    count: int = Field(ge=0)


class ResolveAlertBody(ReasonBody):
    note: str
