"""The authenticated user's notifications (Phase 10).

Every endpoint requires an active user with a known role
(``require_any_role``: 401 without a valid token or for an inactive
account, 403 for an unknown stored role). The recipient is always the
authenticated user; no request carries a recipient, actor or role. A
notification the user may not see is answered exactly like a missing one
(404 "Notification not found."). Notification content is never logged.
"""

import logging
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.api.dependencies import require_any_role
from app.db.database import get_db
from app.db.errors import describe_rollback_failure, rollback_failure_of
from app.models import User
from app.schemas.notification import (
    MarkNotificationsReadRequest,
    MarkNotificationsReadResponse,
    NotificationRead,
    UnreadNotificationSummary,
)
from app.schemas.ticket import MAX_DATABASE_INTEGER
from app.services.notification_service import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    NotificationError,
    NotificationNotFoundError,
    NotificationView,
    get_unread_notification_summary,
    list_notifications,
    mark_notification_read,
    mark_notifications_read,
)

logger = logging.getLogger(__name__)

NOTIFICATION_NOT_FOUND_DETAIL = "Notification not found."
INTERNAL_ERROR_DETAIL = "Internal server error."


# ---------------------------------------------------------------------------
# Error mapping (one place for every notification endpoint)
# ---------------------------------------------------------------------------


def _notification_http_error(error: NotificationError) -> HTTPException:
    """Translate a notification error into a safe, fixed-text response.

    A failed rollback recorded on the error is logged with the
    allow-listed diagnostic only.
    """

    rollback_failure = rollback_failure_of(error)

    if rollback_failure is not None:
        logger.error(
            "Rolling back a rejected notification request failed (%s).",
            describe_rollback_failure(rollback_failure),
        )

    if isinstance(error, NotificationNotFoundError):
        return HTTPException(
            status.HTTP_404_NOT_FOUND,
            NOTIFICATION_NOT_FOUND_DETAIL,
        )

    logger.error("An unexpected notification error occurred.")

    return HTTPException(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        INTERNAL_ERROR_DETAIL,
    )


class NotificationRoute(APIRoute):
    """Route that maps notification errors to HTTP responses.

    Applies to this router only. Database errors still go to the
    application's safe database error handler (a generic 500).
    """

    def get_route_handler(
        self,
    ) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def handler_with_notification_errors(request: Request) -> Response:
            try:
                return await handler(request)
            except NotificationError as error:
                raise _notification_http_error(error) from None

        return handler_with_notification_errors


router = APIRouter(tags=["Notifications"], route_class=NotificationRoute)

NotificationId = Annotated[int, Path(ge=1, le=MAX_DATABASE_INTEGER)]

NotificationCursor = Annotated[
    int | None,
    Query(
        ge=1,
        le=MAX_DATABASE_INTEGER,
        description=(
            "Return only notifications older than this ID (the oldest ID "
            "already loaded)."
        ),
    ),
]


def _notification_read(view: NotificationView) -> NotificationRead:
    """The response for one notification, field by field."""

    return NotificationRead(
        id=view.id,
        notification_type=view.notification_type,
        ticket_id=view.ticket_id,
        ticket_title=view.ticket_title,
        message_id=view.message_id,
        actor_name=view.actor_name,
        actor_role=view.actor_role,
        preview=view.preview,
        created_at=view.created_at,
        read_at=view.read_at,
        is_read=view.is_read,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=list[NotificationRead],
    summary="List your notifications",
)
def list_notifications_endpoint(
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_any_role)],
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    before_id: NotificationCursor = None,
    unread_only: bool = False,
) -> list[NotificationRead]:
    """Return one page of your notifications, newest first.

    Pass the oldest ID already loaded as ``before_id`` to load older ones;
    ``unread_only=true`` returns unread ones only. ``X-Total-Count`` holds
    the number matching the filters (all pages, ignoring the cursor);
    ``X-Unread-Count`` the number of your unread notifications, whatever
    the filters.
    """

    page = list_notifications(
        db,
        viewer=current_user,
        limit=limit,
        before_id=before_id,
        unread_only=unread_only,
    )
    summary = get_unread_notification_summary(db, current_user)

    response.headers["X-Total-Count"] = str(page.total)
    response.headers["X-Unread-Count"] = str(summary.unread_count)

    return [_notification_read(view) for view in page.notifications]


@router.get(
    "/unread-count",
    response_model=UnreadNotificationSummary,
    summary="Count your unread notifications",
)
def unread_notification_summary_endpoint(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_any_role)],
) -> UnreadNotificationSummary:
    """Return your unread count and your newest notification's ID.

    Meant for polling: ``latest_id`` (read or not, null when you have
    none) changes only when a new notification arrives.
    """

    summary = get_unread_notification_summary(db, current_user)

    return UnreadNotificationSummary(
        unread_count=summary.unread_count,
        latest_id=summary.latest_id,
    )


@router.post(
    "/read-all",
    response_model=MarkNotificationsReadResponse,
    summary="Mark your notifications read",
)
def mark_notifications_read_endpoint(
    payload: MarkNotificationsReadRequest,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_any_role)],
) -> MarkNotificationsReadResponse:
    """Mark your unread notifications read.

    ``{}`` marks all of them; ``up_to_id`` only those up to that ID (send
    the newest one you have shown, so later arrivals stay unread);
    ``ticket_id`` only those on one ticket. Notifications already read
    keep their original ``read_at``.
    """

    result = mark_notifications_read(
        db,
        current_user,
        up_to_id=payload.up_to_id,
        ticket_id=payload.ticket_id,
    )

    return MarkNotificationsReadResponse(
        marked_read=result.marked_read,
        unread_count=result.unread_count,
    )


@router.post(
    "/{notification_id}/read",
    response_model=NotificationRead,
    summary="Mark one notification read",
)
def mark_notification_read_endpoint(
    notification_id: NotificationId,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_any_role)],
) -> NotificationRead:
    """Mark one of your notifications read and return it.

    Repeating the request changes nothing: the original ``read_at`` is
    kept. Another user's notification returns the same 404 as a missing
    one.
    """

    view = mark_notification_read(db, current_user, notification_id)

    return _notification_read(view)
