"""Request and response bodies of the notification API (Phase 10).

Responses carry IDs, the ticket title, the actor's display name and role
snapshot, a short preview and timestamps only: never an email, password
hash, token or internal database object. Requests never carry a
recipient, actor or role: those always come from the authenticated user.
"""

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.schemas.ticket import MAX_DATABASE_INTEGER


# A database ID: a JSON integer only ("3", 3.0 and true are rejected).
DatabaseId = Annotated[
    int,
    Field(strict=True, ge=1, le=MAX_DATABASE_INTEGER),
]

Count = Annotated[int, Field(strict=True, ge=0)]

# The stored values (app.models.notification.NOTIFICATION_TYPES and
# app.models.ticket_message.MESSAGE_SENDER_ROLES).
NotificationType = Literal["ticket_message"]

ActorRole = Literal["admin", "agent", "customer"]

# Match the column lengths (tickets.title, users.name) and the service's
# PREVIEW_LENGTH.
TICKET_TITLE_MAX_LENGTH = 200
ACTOR_NAME_MAX_LENGTH = 100
PREVIEW_MAX_LENGTH = 140


class _Response(BaseModel):
    """Responses are built field by field from service results."""

    model_config = ConfigDict(strict=True, extra="forbid")


class NotificationRead(_Response):
    """One notification as its recipient sees it."""

    id: DatabaseId

    notification_type: NotificationType

    ticket_id: DatabaseId

    ticket_title: Annotated[str, Field(max_length=TICKET_TITLE_MAX_LENGTH)]

    # Always present for ``ticket_message`` notifications.
    message_id: DatabaseId | None

    actor_name: Annotated[str, Field(max_length=ACTOR_NAME_MAX_LENGTH)]

    # The sender's role when the message was sent (a snapshot).
    actor_role: ActorRole | None

    # Plain text, as written; clients must display it as text.
    preview: Annotated[str, Field(max_length=PREVIEW_MAX_LENGTH)]

    created_at: AwareDatetime

    read_at: AwareDatetime | None

    is_read: bool

    @model_validator(mode="after")
    def _read_state_is_consistent(self) -> "NotificationRead":
        if self.is_read != (self.read_at is not None):
            raise ValueError("is_read must match read_at.")

        return self


class UnreadNotificationSummary(_Response):
    """For polling: the unread count and the newest visible notification ID."""

    unread_count: Count

    # The newest visible notification, read or not; null when there is none.
    latest_id: DatabaseId | None


class MarkNotificationsReadRequest(BaseModel):
    """Which unread notifications to mark read.

    Both filters are optional and combine: ``up_to_id`` limits it to IDs up
    to and including the newest one the client has shown (later ones stay
    unread), ``ticket_id`` to one ticket. An empty object marks every
    visible notification read. Any other field is rejected.
    """

    model_config = ConfigDict(extra="forbid")

    up_to_id: DatabaseId | None = None

    ticket_id: DatabaseId | None = None


class MarkNotificationsReadResponse(_Response):
    """How many were newly marked read, and how many are still unread."""

    marked_read: Count

    unread_count: Count


__all__ = [
    "MarkNotificationsReadRequest",
    "MarkNotificationsReadResponse",
    "NotificationRead",
    "UnreadNotificationSummary",
]
