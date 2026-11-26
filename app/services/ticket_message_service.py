"""Ticket conversations: reading and sending messages.

Business rules (Phase 9):

* Reading. Active administrators and agents can read the conversation of
  every ticket (the shared support queue); a customer only that of a
  ticket they created. Any other ticket is reported exactly like a
  missing one (``TicketNotFoundError``), so customers cannot discover
  other customers' tickets.
* Sending. The sender is always the acting user, and their role is
  stored with the message as a snapshot. Checks, in order:

  1. the actor exists and is active (``TicketPermissionError``);
  2. the ticket exists, and a customer created it
     (``TicketNotFoundError`` otherwise, the same for both);
  3. the ticket is not Resolved (``TicketResolvedError``: a resolved
     ticket is read-only for everyone until staff reopen it through the
     status workflow);
  4. an agent is the ticket's current assignee (``TicketPermissionError``
     otherwise). Administrators can write on any unresolved ticket,
     assigned or not.

* Messages are append-only. They never change the ticket row (so its
  optimistic ``version`` is untouched) and never create a
  ``ticket_events`` row.
* Notifications (Phase 10). Each new message notifies its recipients
  (``app.services.notification_service.resolve_message_recipients``),
  written in the message's own transaction: the message and its
  notifications are committed together or not at all.

Transactions. ``create_ticket_message`` owns the session's transaction.
It starts it with the shared serialised-change lock (``app.db.locking``:
SQLite's ``BEGIN IMMEDIATE``), re-reads the acting user and the ticket
inside it (``SELECT ... FOR UPDATE`` where supported, user row before
ticket row, the global lock order of ``app.services.ticket_workflow``),
checks every rule, inserts the message, then reads the recipient context
(``_load_recipient_context``: the assignee, the creator and the active
administrator IDs, with plain reads and NO row locks, so the global lock
order of administrators, then users, then tickets is never reversed) and
inserts the notifications, and commits. A concurrent unassignment,
deactivation, role change or resolution therefore either commits before
the checks (and the message is refused, or notifies whoever is assigned
and active after it) or waits until the message and its notifications
have committed. On ANY failure, including a failed notification insert,
the transaction is rolled back (neither the message nor any notification
is kept; no savepoint) and the original error re-raised; a failed
rollback is attached as a secondary failure
(``app.db.errors.rollback_failure_of``).

Errors carry fixed messages only. Message content is never logged and
never placed in an error.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.core.roles import UserRole
from app.db import locking as _locking
from app.db.errors import roll_back_keeping_primary
from app.models import Ticket, TicketMessage, User
from app.models.ticket_message import MAX_MESSAGE_LENGTH
from app.services.notification_service import (
    add_message_notifications,
    resolve_message_recipients,
)
from app.services.ticket_workflow import (
    STATUS_RESOLVED,
    TicketNotFoundError,
    TicketPermissionError,
    TicketWorkflowError,
)


DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100

# Roles that read every ticket's conversation (the shared queue).
_STAFF_ROLES: frozenset[UserRole] = frozenset({UserRole.ADMIN, UserRole.AGENT})


# ---------------------------------------------------------------------------
# Errors (fixed messages only)
# ---------------------------------------------------------------------------


class TicketResolvedError(TicketWorkflowError):
    """A message was sent to a Resolved ticket."""

    def __init__(self) -> None:
        super().__init__(
            "This ticket is resolved. It must be reopened before another "
            "message can be sent."
        )


class InvalidTicketMessageError(TicketWorkflowError, ValueError):
    """The message content is not acceptable (empty, blank or too long).

    The API's request schema rejects such content first; this guards
    every other caller. The message never repeats the content.
    """

    def __init__(self) -> None:
        super().__init__(
            "Message content must be 1 to "
            f"{MAX_MESSAGE_LENGTH:,} characters and not only whitespace."
        )


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TicketMessageView:
    """A message with its sender's display name, safe to return.

    Built from loaded rows (inside the transaction, for a new message), so
    callers never read the database again after a commit. Holds no email,
    password hash or token data.
    """

    id: int
    ticket_id: int
    sender_user_id: int
    sender_name: str
    sender_role: str
    content: str
    created_at: datetime


@dataclass(frozen=True)
class TicketMessagePage:
    """One page of a conversation, newest first.

    ``total`` is the number of messages on the ticket (all pages).
    """

    messages: list[TicketMessageView]
    total: int


def _as_utc(value: datetime) -> datetime:
    """``value`` as an aware UTC datetime.

    SQLite returns stored timestamps without their offset; every one was
    written in UTC, so it is labelled as UTC. Responses are then the same
    whether the message was just created or read back later.
    """

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)

    return value.astimezone(timezone.utc)


def _view(message: TicketMessage, sender_name: str) -> TicketMessageView:
    return TicketMessageView(
        id=message.id,
        ticket_id=message.ticket_id,
        sender_user_id=message.sender_user_id,
        sender_name=sender_name,
        sender_role=message.sender_role,
        content=message.content,
        created_at=_as_utc(message.created_at),
    )


# ---------------------------------------------------------------------------
# Permission rules
# ---------------------------------------------------------------------------


def _role_of(value: object) -> UserRole | None:
    """The ``UserRole`` for an exact stored role value, else None."""

    if isinstance(value, UserRole):
        return value

    try:
        return UserRole(value)
    except ValueError:
        return None


def can_read_conversation(user: User | None, ticket: Ticket) -> bool:
    """Whether ``user`` may read ``ticket``'s conversation.

    Active staff read every ticket; an active customer only their own.
    Inactive users and unknown roles read nothing (fails closed).
    """

    if user is None or not user.is_active:
        return False

    role = _role_of(user.role)

    if role in _STAFF_ROLES:
        return True

    return role is UserRole.CUSTOMER and ticket.created_by_user_id == user.id


def check_can_send_message(user: User | None, ticket: Ticket | None) -> UserRole:
    """Apply the sending rules (see the module docstring); return the role.

    Raises:
        TicketPermissionError: the user is missing, inactive or has an
            unknown role, or is an agent who is not the ticket's assignee.
        TicketNotFoundError: the ticket is missing, or a customer did not
            create it.
        TicketResolvedError: the ticket is Resolved.
    """

    if user is None or not user.is_active:
        raise TicketPermissionError()

    role = _role_of(user.role)

    if role is None:
        raise TicketPermissionError()

    if ticket is None:
        raise TicketNotFoundError()

    if role is UserRole.CUSTOMER and ticket.created_by_user_id != user.id:
        raise TicketNotFoundError()

    if ticket.status == STATUS_RESOLVED:
        raise TicketResolvedError()

    if role is UserRole.AGENT and (
        ticket.assigned_to_user_id is None
        or ticket.assigned_to_user_id != user.id
    ):
        raise TicketPermissionError()

    return role


def normalise_message_content(content: object) -> str:
    """Strip ``content`` and check it; return the text to store.

    Raises:
        InvalidTicketMessageError: not a string, empty or whitespace-only
            after stripping, or longer than ``MAX_MESSAGE_LENGTH``.
    """

    if not isinstance(content, str):
        raise InvalidTicketMessageError()

    text = content.strip()

    if not text or len(text) > MAX_MESSAGE_LENGTH:
        raise InvalidTicketMessageError()

    return text


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _check_page_arguments(limit: object, before_id: object) -> None:
    for value in (limit, before_id):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int)
        ):
            raise TypeError("limit and before_id must be integers.")

    if limit is None or not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}.")

    if before_id is not None and before_id < 1:
        raise ValueError("before_id must be a positive integer.")


def list_ticket_messages(
    db: Session,
    *,
    viewer: User,
    ticket_id: int,
    limit: int = DEFAULT_PAGE_SIZE,
    before_id: int | None = None,
) -> TicketMessagePage:
    """One page of a ticket's conversation, newest first.

    Messages are ordered by ID, descending (IDs follow insertion order and
    messages are never edited, so the order is stable). Without
    ``before_id`` the newest ``limit`` messages are returned; with it, the
    newest ``limit`` messages whose ID is lower (older), which makes
    ``before_id`` = the oldest ID already shown a stable cursor even while
    new messages arrive.

    Raises:
        TicketNotFoundError: the ticket does not exist or ``viewer`` may
            not read it (``can_read_conversation``).
        TypeError, ValueError: invalid ``limit`` or ``before_id``.
    """

    _check_page_arguments(limit, before_id)

    ticket = db.get(Ticket, ticket_id)

    if ticket is None or not can_read_conversation(viewer, ticket):
        raise TicketNotFoundError()

    conditions = [TicketMessage.ticket_id == ticket_id]

    if before_id is not None:
        conditions.append(TicketMessage.id < before_id)

    statement = (
        select(TicketMessage, User.name)
        .join(User, User.id == TicketMessage.sender_user_id)
        .where(*conditions)
        .order_by(TicketMessage.id.desc())
        .limit(limit)
    )

    messages = [
        _view(message, sender_name)
        for message, sender_name in db.execute(statement).all()
    ]

    total = db.scalar(
        select(func.count())
        .select_from(TicketMessage)
        .where(TicketMessage.ticket_id == ticket_id)
    )

    return TicketMessagePage(messages=messages, total=int(total or 0))


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def _user_for_update(user_id: int) -> Select:
    return (
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _ticket_for_update(ticket_id: int) -> Select:
    return (
        select(Ticket)
        .where(Ticket.id == ticket_id)
        .with_for_update()
        # Overwrite any stale copy already in the session.
        .execution_options(populate_existing=True)
    )


def _lock_for_message(
    db: Session,
    *,
    actor_id: int,
    ticket_id: int,
) -> tuple[User | None, Ticket | None]:
    """Take the lock, then read the actor and the ticket (in that order).

    A module-level function so tests can hold a message inside its lock.
    """

    _locking.begin_serialised_change(db)

    actor = db.scalars(_user_for_update(actor_id)).one_or_none()
    ticket = db.scalars(_ticket_for_update(ticket_id)).one_or_none()

    return actor, ticket


def _active_admin_ids() -> Select:
    """IDs of the active administrators, in id order. Never locks rows."""

    return (
        select(User.id)
        .where(
            User.role == UserRole.ADMIN.value,
            User.is_active.is_(True),
        )
        .order_by(User.id)
    )


def _user_by_id(user_id: int) -> Select:
    """One user row, refreshed from the database. Never locks the row."""

    return (
        select(User)
        .where(User.id == user_id)
        # Overwrite any stale copy already in the session.
        .execution_options(populate_existing=True)
    )


def _load_recipient_context(
    db: Session,
    ticket: Ticket,
) -> tuple[User | None, User | None, list[int]]:
    """Read who a message on ``ticket`` may notify: (assignee, creator, admins).

    Runs inside the message's serialised transaction, after the actor and
    ticket rows were locked. Every read is a plain SELECT: locking user
    rows here, after the ticket, would reverse the global lock order
    (``app.services.user_locking``). On SQLite the transaction's
    ``BEGIN IMMEDIATE`` already makes these reads consistent; elsewhere a
    concurrent change can at worst affect who is notified, never whether
    the message is allowed (that was decided on the locked rows).

    The ticket's own ``assigned_to_user_id`` and ``created_by_user_id``
    are the user IDs read. Whether each user is active and has the
    expected role is checked by ``resolve_message_recipients``. A module-
    level function so tests can observe or replace it.
    """

    assignee = (
        None
        if ticket.assigned_to_user_id is None
        else db.scalars(_user_by_id(ticket.assigned_to_user_id)).one_or_none()
    )
    creator = db.scalars(_user_by_id(ticket.created_by_user_id)).one_or_none()
    admin_ids = list(db.scalars(_active_admin_ids()))

    return assignee, creator, admin_ids


def create_ticket_message(
    db: Session,
    *,
    actor_id: int,
    ticket_id: int,
    content: str,
) -> TicketMessageView:
    """Add a message from ``actor_id`` to the ticket's conversation.

    ``content`` is stripped and checked before any database work. Inside
    the serialised transaction the actor and ticket are re-read and the
    sending rules applied (``check_can_send_message``); then the message
    is inserted with the actor's ID and current role, its notifications
    are added (recipients from ``resolve_message_recipients``, using the
    role re-read here), and both are committed together. A valid message
    with nobody to notify is committed on its own.

    Returns the new message, captured inside the transaction.

    Raises:
        InvalidTicketMessageError, TicketPermissionError,
        TicketNotFoundError, TicketResolvedError,
        sqlalchemy.exc.SQLAlchemyError (including a lock time-out).
        Nothing is written when any of these is raised, including when
        a notification cannot be inserted.
    """

    text = normalise_message_content(content)

    try:
        actor, ticket = _lock_for_message(
            db,
            actor_id=actor_id,
            ticket_id=ticket_id,
        )
        role = check_can_send_message(actor, ticket)

        message = TicketMessage(
            ticket_id=ticket.id,
            sender_user_id=actor.id,
            sender_role=role.value,
            content=text,
            created_at=datetime.now(timezone.utc),
        )
        db.add(message)
        # Assigns the ID; a constraint failure surfaces here, before commit.
        db.flush()

        # Notifications, in the same transaction (plain reads, no locks).
        assignee, creator, admin_ids = _load_recipient_context(db, ticket)
        recipients = resolve_message_recipients(
            role,
            actor.id,
            ticket,
            assignee,
            creator,
            admin_ids,
        )
        add_message_notifications(db, message, recipients)

        view = _view(message, actor.name)
        db.commit()

    except Exception as caught:
        primary = roll_back_keeping_primary(db, caught)

        if primary is caught:
            raise

        raise primary from None

    return view


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "InvalidTicketMessageError",
    "TicketMessagePage",
    "TicketMessageView",
    "TicketResolvedError",
    "can_read_conversation",
    "check_can_send_message",
    "create_ticket_message",
    "list_ticket_messages",
    "normalise_message_content",
]
