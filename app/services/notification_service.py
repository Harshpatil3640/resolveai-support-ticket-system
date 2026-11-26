"""Notifications: who is notified of a message, and reading them back.

Business rules (Phase 10):

* Recipients of a new conversation message (``resolve_message_recipients``,
  a pure function):

  - a customer writes: the ticket's assignee if it is an active agent;
    otherwise (unassigned, or the assignee is inactive or no longer an
    agent) every active administrator;
  - an agent writes: the ticket's creator, if it is an active customer;
  - an administrator writes: the ticket's creator, if it is an active
    customer, and the ticket's assignee, if it is an active agent.

  The sender is never notified, nobody is notified twice, and anything
  unexpected (an unknown role, a user row that is not the ticket's
  creator or assignee) notifies nobody rather than someone wrong. All
  agents are never notified of an unassigned ticket: the queue serves
  that purpose.

* Visibility (``visible_notifications_condition``, used by EVERY read and
  update here). A viewer only ever sees notifications addressed to them,
  and only while they are active with a known role. Active administrators
  and agents see all of their own; an active customer only those on
  tickets they created, so a demoted agent loses sight of other
  customers' tickets. A notification the viewer may not see is treated
  exactly like a missing one (``NotificationNotFoundError``).

* A read notification never becomes unread again: ``read_at`` is only
  written by conditional updates ``WHERE read_at IS NULL``, and nothing
  here clears it.

Transactions. ``add_message_notifications`` works inside the caller's
transaction: it adds and flushes rows, and never begins, commits or rolls
back. The listing and summary functions only read. ``mark_notification_read``
and ``mark_notifications_read`` own the session's transaction: they start
it with the shared serialised-change lock (``app.db.locking``: SQLite's
``BEGIN IMMEDIATE``), so their update and the state they return are one
consistent snapshot, then commit; on ANY failure they roll back and
re-raise the original error (a failed rollback is attached as a secondary
failure, ``app.db.errors.rollback_failure_of``). They lock no user or
ticket rows, and only update notification rows, so they cannot conflict
with the global user-then-ticket lock order of
``app.services.ticket_workflow``.

Message content is never stored in a notification, never logged and
never placed in an error. Errors carry fixed messages only.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import ColumnElement, Select, and_, false, func, select, update
from sqlalchemy.orm import Session

from app.core.roles import UserRole
from app.db import locking as _locking
from app.db.errors import roll_back_keeping_primary
from app.models import Notification, Ticket, TicketMessage, User
from app.models.notification import NOTIFICATION_TYPE_TICKET_MESSAGE


DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 50

# Longest message preview returned, in characters (ellipsis included).
PREVIEW_LENGTH = 140
_ELLIPSIS = "…"

# Roles that see every notification addressed to them.
_STAFF_ROLES: frozenset[UserRole] = frozenset({UserRole.ADMIN, UserRole.AGENT})


# ---------------------------------------------------------------------------
# Errors (fixed messages only)
# ---------------------------------------------------------------------------


class NotificationError(Exception):
    """Base class for notification errors."""


class NotificationNotFoundError(NotificationError, LookupError):
    """The notification does not exist, or the viewer may not see it.

    Both cases raise this same error with the same message, so callers
    cannot tell another user's notification from a missing one.
    """

    def __init__(self) -> None:
        super().__init__("Notification not found.")


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NotificationView:
    """A notification with what a recipient needs to show it, safe to return.

    Built from loaded rows, so callers never read the database again
    after a commit. Holds the actor's display name and role snapshot
    only: no email, password hash or token data.
    """

    id: int
    notification_type: str
    ticket_id: int
    ticket_title: str
    message_id: int | None
    actor_name: str
    actor_role: str | None
    preview: str
    created_at: datetime
    read_at: datetime | None
    is_read: bool


@dataclass(frozen=True)
class NotificationPage:
    """One page of a viewer's notifications, newest first.

    ``total`` counts every visible notification matching the filters
    (``unread_only``) on all pages, ignoring the cursor.
    """

    notifications: list[NotificationView]
    total: int


@dataclass(frozen=True)
class NotificationSummary:
    """What polling needs: the unread count and the newest visible ID."""

    unread_count: int
    latest_id: int | None


@dataclass(frozen=True)
class MarkReadResult:
    """Outcome of marking several notifications read."""

    marked_read: int
    unread_count: int


def _as_utc(value: datetime | None) -> datetime | None:
    """``value`` as an aware UTC datetime (SQLite drops the offset)."""

    if value is None:
        return None

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)

    return value.astimezone(timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def make_preview(content: str | None) -> str:
    """A plain-text, single-line preview of at most ``PREVIEW_LENGTH`` chars.

    Runs of whitespace (including line breaks) become one space. Longer
    text is cut and ends with an ellipsis. The text is not interpreted
    (no HTML handling): it is returned as the sender wrote it.
    """

    if not content:
        return ""

    text = " ".join(content.split())

    if len(text) <= PREVIEW_LENGTH:
        return text

    return text[: PREVIEW_LENGTH - 1].rstrip() + _ELLIPSIS


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


def _role_of(value: object) -> UserRole | None:
    """The ``UserRole`` for an exact stored role value, else None."""

    if isinstance(value, UserRole):
        return value

    try:
        return UserRole(value)
    except ValueError:
        return None


def _has_active_role(user: object | None, role: UserRole) -> bool:
    return (
        user is not None
        and getattr(user, "is_active", False) is True
        and _role_of(getattr(user, "role", None)) is role
    )


# ---------------------------------------------------------------------------
# Recipients (pure)
# ---------------------------------------------------------------------------


def resolve_message_recipients(
    sender_role: UserRole | str | None,
    sender_id: int,
    ticket: Ticket,
    assignee: User | None,
    creator: User | None,
    active_admin_ids: Iterable[int],
) -> set[int]:
    """The IDs to notify of a message ``sender_id`` wrote on ``ticket``.

    Pure: reads only the given objects' attributes, never the database.
    ``assignee`` and ``creator`` are the current user rows for the
    ticket's ``assigned_to_user_id`` and ``created_by_user_id`` (None if
    there is none); a row whose ID does not match the ticket is ignored.
    ``active_admin_ids`` are the IDs of every active administrator.

    See the module docstring for the rules. The sender is always removed
    and the result has no duplicates. An unknown sender role, or a
    customer sender who did not create the ticket, notifies nobody.
    """

    role = _role_of(sender_role)

    if role is None:
        return set()

    assigned_agent = (
        assignee is not None
        and ticket.assigned_to_user_id is not None
        and assignee.id == ticket.assigned_to_user_id
        and _has_active_role(assignee, UserRole.AGENT)
    )
    customer_creator = (
        creator is not None
        and creator.id == ticket.created_by_user_id
        and _has_active_role(creator, UserRole.CUSTOMER)
    )

    recipients: set[int] = set()

    if role is UserRole.CUSTOMER:
        # Only the ticket's own customer can write as a customer.
        if ticket.created_by_user_id != sender_id:
            return set()

        if assigned_agent:
            recipients.add(assignee.id)
        else:
            recipients.update(active_admin_ids)

    elif role is UserRole.AGENT:
        if customer_creator:
            recipients.add(creator.id)

    elif role is UserRole.ADMIN:
        if customer_creator:
            recipients.add(creator.id)

        if assigned_agent:
            recipients.add(assignee.id)

    recipients.discard(sender_id)

    return recipients


# ---------------------------------------------------------------------------
# Creating notifications (inside the caller's transaction)
# ---------------------------------------------------------------------------


def _check_id(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer.")

    if value < 1:
        raise ValueError(f"{name} must be a positive integer.")


def add_message_notifications(
    db: Session,
    message: TicketMessage,
    recipient_ids: Iterable[int],
) -> list[Notification]:
    """Add one ``ticket_message`` notification per recipient of ``message``.

    ``message`` must already be flushed (it has an ID). Every reference is
    taken from the message itself: the ticket (``message.ticket_id``), the
    actor (``message.sender_user_id``) and the time (``message.created_at``),
    so a notification can never point at a different ticket than its
    message. The sender is skipped, duplicates are removed, and rows are
    added in recipient-ID order. No content is copied.

    The rows are flushed, so a constraint or foreign-key failure surfaces
    here. Never begins, commits or rolls back: the caller owns the
    transaction and must roll back on any failure, which also removes
    these rows.

    Returns the added notifications (empty when nobody is notified).

    Raises:
        ValueError: ``message`` has not been flushed, or lacks a ticket,
            sender or time. Nothing was added.
        TypeError, ValueError: a recipient ID is not a positive integer.
            Nothing was added.
        sqlalchemy.exc.SQLAlchemyError: the flush failed; the caller must
            roll back.
    """

    if (
        message.id is None
        or message.ticket_id is None
        or message.sender_user_id is None
        or message.created_at is None
    ):
        raise ValueError("The message must be flushed before it is notified.")

    recipients = set()

    for recipient_id in recipient_ids:
        _check_id(recipient_id, "recipient_ids")
        recipients.add(recipient_id)

    recipients.discard(message.sender_user_id)

    notifications = [
        Notification(
            recipient_user_id=recipient_id,
            actor_user_id=message.sender_user_id,
            ticket_id=message.ticket_id,
            message_id=message.id,
            notification_type=NOTIFICATION_TYPE_TICKET_MESSAGE,
            created_at=message.created_at,
        )
        for recipient_id in sorted(recipients)
    ]

    if notifications:
        db.add_all(notifications)
        db.flush()

    return notifications


# ---------------------------------------------------------------------------
# Visibility (every read and update below)
# ---------------------------------------------------------------------------


def visible_notifications_condition(viewer: User | None) -> ColumnElement[bool]:
    """SQL condition: the notifications ``viewer`` may see.

    Always restricted to ``recipient_user_id == viewer.id``. Active staff
    see all of those; an active customer only those on tickets they
    created. Missing or inactive viewers and unknown roles see nothing
    (fails closed). Uses no join, so it works in SELECT and UPDATE alike.
    """

    if viewer is None or viewer.is_active is not True or viewer.id is None:
        return false()

    role = _role_of(viewer.role)

    if role is None:
        return false()

    own = Notification.recipient_user_id == viewer.id

    if role in _STAFF_ROLES:
        return own

    return and_(
        own,
        Notification.ticket_id.in_(
            select(Ticket.id).where(Ticket.created_by_user_id == viewer.id)
        ),
    )


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _check_page_arguments(limit: object, before_id: object, unread_only: object) -> None:
    for value in (limit, before_id):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int)
        ):
            raise TypeError("limit and before_id must be integers.")

    if not isinstance(unread_only, bool):
        raise TypeError("unread_only must be a boolean.")

    if limit is None or not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}.")

    if before_id is not None and before_id < 1:
        raise ValueError("before_id must be a positive integer.")


def _views_query(*conditions: ColumnElement[bool]) -> Select:
    """Notifications with their ticket title, actor name and message."""

    return (
        select(
            Notification,
            Ticket.title,
            User.name,
            TicketMessage.sender_role,
            TicketMessage.content,
        )
        .join(Ticket, Ticket.id == Notification.ticket_id)
        .join(User, User.id == Notification.actor_user_id)
        .outerjoin(TicketMessage, TicketMessage.id == Notification.message_id)
        .where(*conditions)
        .order_by(Notification.id.desc())
        # A notification already in the session is refreshed from the row.
        .execution_options(populate_existing=True)
    )


def _view(row) -> NotificationView:
    notification, ticket_title, actor_name, actor_role, content = row
    read_at = _as_utc(notification.read_at)

    return NotificationView(
        id=notification.id,
        notification_type=notification.notification_type,
        ticket_id=notification.ticket_id,
        ticket_title=ticket_title,
        message_id=notification.message_id,
        actor_name=actor_name,
        actor_role=actor_role,
        preview=make_preview(content),
        created_at=_as_utc(notification.created_at),
        read_at=read_at,
        is_read=read_at is not None,
    )


def _count(db: Session, *conditions: ColumnElement[bool]) -> int:
    return int(
        db.scalar(
            select(func.count()).select_from(Notification).where(*conditions)
        )
        or 0
    )


def _unread_count(db: Session, viewer: User) -> int:
    return _count(
        db,
        visible_notifications_condition(viewer),
        Notification.read_at.is_(None),
    )


def list_notifications(
    db: Session,
    *,
    viewer: User,
    limit: int = DEFAULT_PAGE_SIZE,
    before_id: int | None = None,
    unread_only: bool = False,
) -> NotificationPage:
    """One page of ``viewer``'s visible notifications, newest first.

    Ordered by ID, descending (IDs follow insertion order). With
    ``before_id`` only notifications with a lower ID are returned, so the
    oldest ID already shown is a stable cursor while new ones arrive.
    ``unread_only`` keeps unread ones only. Reads only.

    Raises:
        TypeError, ValueError: invalid ``limit``, ``before_id`` or
            ``unread_only``.
    """

    _check_page_arguments(limit, before_id, unread_only)

    filters = [visible_notifications_condition(viewer)]

    if unread_only:
        filters.append(Notification.read_at.is_(None))

    page_filters = list(filters)

    if before_id is not None:
        page_filters.append(Notification.id < before_id)

    notifications = [
        _view(row)
        for row in db.execute(_views_query(*page_filters).limit(limit)).all()
    ]

    return NotificationPage(
        notifications=notifications,
        total=_count(db, *filters),
    )


def get_unread_notification_summary(
    db: Session,
    viewer: User,
) -> NotificationSummary:
    """The viewer's visible unread count and newest visible ID (read or not).

    Reads only. ``latest_id`` is None when the viewer has no visible
    notifications.
    """

    latest_id = db.scalar(
        select(func.max(Notification.id)).where(
            visible_notifications_condition(viewer)
        )
    )

    return NotificationSummary(
        unread_count=_unread_count(db, viewer),
        latest_id=None if latest_id is None else int(latest_id),
    )


# ---------------------------------------------------------------------------
# Marking read
# ---------------------------------------------------------------------------


def _begin_change(db: Session) -> None:
    """Start the write-locked transaction (shared ``app.db.locking`` helper).

    Called through this module-level name so tests can hold a change
    inside its lock.
    """

    _locking.begin_serialised_change(db)


def _mark_read_statement(*conditions: ColumnElement[bool]):
    """UPDATE that sets read_at only where it is still NULL."""

    return (
        update(Notification)
        .where(*conditions, Notification.read_at.is_(None))
        .values(read_at=_now())
        # Session objects are expired by the commit; nothing to evaluate.
        .execution_options(synchronize_session=False)
    )


def mark_notification_read(
    db: Session,
    viewer: User,
    notification_id: int,
) -> NotificationView:
    """Mark one of ``viewer``'s notifications read; return its current state.

    Idempotent: a notification already read keeps its original
    ``read_at``. Inside the serialised transaction the notification is
    looked up with the visibility condition, updated only where
    ``read_at IS NULL``, re-read and committed.

    Raises:
        TypeError, ValueError: ``notification_id`` is not a positive
            integer (before any database work).
        NotificationNotFoundError: missing, or not visible to ``viewer``
            (the same error for both).
        sqlalchemy.exc.SQLAlchemyError (including a lock time-out).
        Nothing is changed when any of these is raised.
    """

    _check_id(notification_id, "notification_id")

    try:
        _begin_change(db)

        visible = and_(
            Notification.id == notification_id,
            visible_notifications_condition(viewer),
        )

        if db.scalar(select(Notification.id).where(visible)) is None:
            raise NotificationNotFoundError()

        db.execute(_mark_read_statement(visible))

        view = _view(db.execute(_views_query(visible)).one())
        db.commit()

    except Exception as caught:
        primary = roll_back_keeping_primary(db, caught)

        if primary is caught:
            raise

        raise primary from None

    return view


def mark_notifications_read(
    db: Session,
    viewer: User,
    up_to_id: int | None = None,
    ticket_id: int | None = None,
) -> MarkReadResult:
    """Mark ``viewer``'s unread visible notifications read.

    ``up_to_id`` limits it to IDs up to and including that one (the
    newest the viewer has actually seen), so notifications that arrive
    later stay unread; ``ticket_id`` limits it to one ticket. Both may be
    combined; neither means all. Rows already read keep their
    ``read_at``. A ticket the viewer cannot see simply matches nothing.

    Returns how many were newly marked read and the unread count after
    the change, both from the same serialised transaction.

    Raises:
        TypeError, ValueError: a filter is not a positive integer (before
            any database work).
        sqlalchemy.exc.SQLAlchemyError (including a lock time-out).
        Nothing is changed when any of these is raised.
    """

    if up_to_id is not None:
        _check_id(up_to_id, "up_to_id")

    if ticket_id is not None:
        _check_id(ticket_id, "ticket_id")

    conditions = [visible_notifications_condition(viewer)]

    if up_to_id is not None:
        conditions.append(Notification.id <= up_to_id)

    if ticket_id is not None:
        conditions.append(Notification.ticket_id == ticket_id)

    try:
        _begin_change(db)

        marked = db.execute(_mark_read_statement(*conditions)).rowcount
        unread = _unread_count(db, viewer)
        db.commit()

    except Exception as caught:
        primary = roll_back_keeping_primary(db, caught)

        if primary is caught:
            raise

        raise primary from None

    return MarkReadResult(marked_read=int(marked or 0), unread_count=unread)


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "PREVIEW_LENGTH",
    "MarkReadResult",
    "NotificationError",
    "NotificationNotFoundError",
    "NotificationPage",
    "NotificationSummary",
    "NotificationView",
    "add_message_notifications",
    "get_unread_notification_summary",
    "list_notifications",
    "make_preview",
    "mark_notification_read",
    "mark_notifications_read",
    "resolve_message_recipients",
    "visible_notifications_condition",
]
