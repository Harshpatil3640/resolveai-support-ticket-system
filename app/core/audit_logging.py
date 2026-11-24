"""Audit log for administrative actions.

Python only emits WARNING and above from a logger nobody configured, and
Uvicorn's default logging configuration configures only its own
"uvicorn.*" loggers, so an INFO audit record would be silently dropped in
a normal deployment. ``configure_audit_logging()`` prevents that, while
respecting any configuration an operator gives the ``resolveai.audit``
logger (for example with ``uvicorn --log-config`` or
``logging.config.dictConfig``):

* The logger is always re-enabled, so a configuration that uses
  ``disable_existing_loggers`` cannot silence the audit log by accident.
* Its level is set to INFO only while it is NOTSET. A level the operator
  chose (e.g. WARNING) is kept.
* Only when the logger has NO handlers is the default handler installed:
  one handler that writes to standard error (where Uvicorn writes its own
  server log) with UTC ISO-8601 timestamps such as
  ``2026-11-22T08:30:00.000Z``. ``propagate`` is set to False together
  with it, so a root handler cannot print each record a second time.
* Handlers an operator configured, and the logger's ``propagate``
  setting, are never removed or changed, and the default handler is never
  added next to them (nor re-added after they replaced it).

The function is idempotent and thread-safe. It runs at application
start-up and again before every audit record
(``app.api.routes.admin.log_admin_action``), so the admin router also
logs correctly in an application that never imports ``app.main``.

Operator recipes for the ``resolveai.audit`` logger:

* Own destinations: configure its ``handlers`` (and ``propagate`` as
  wished). The default handler is not installed.
* Root pipeline only: ``handlers: [<a logging.NullHandler>]`` with
  ``propagate: true``. A logger without any handler gets the default
  handler instead.
* Quieter: set its ``level`` above INFO.

A configuration applied after the application was imported replaces the
default handler, but ``dictConfig`` keeps the previous ``propagate`` value
(False, set together with the default handler) when the key is omitted,
so such a configuration should state ``propagate`` explicitly.

Records are written only through the fixed-field helpers at the end of
this module (``log_admin_action`` and the ``log_ticket_*`` functions),
which log safe fields only (IDs, action names, role and status values).
"""

import logging
import re
import sys
import threading
import time
from typing import TextIO


AUDIT_LOGGER_NAME = "resolveai.audit"
AUDIT_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# Marks the default handler, so it is recognised even if this module is
# imported again (a module reload creates a new handler class).
_DEFAULT_HANDLER_MARKER = "resolveai_audit_handler"

# Serialises the check-then-install in configure_audit_logging().
_configure_lock = threading.Lock()


class UtcIsoFormatter(logging.Formatter):
    """Formats ``%(asctime)s`` as ISO-8601 UTC with milliseconds.

    For example ``2026-11-22T08:30:00.000Z``: independent of the server's
    time zone and daylight saving time, sortable, and accepted by log
    aggregators. Operators can reference it from a logging configuration
    (``"()": "app.core.audit_logging.UtcIsoFormatter"``).
    """

    converter = time.gmtime
    default_time_format = "%Y-%m-%dT%H:%M:%S"
    default_msec_format = "%s.%03dZ"


class _CurrentStderrHandler(logging.StreamHandler):
    """Write each record to whatever ``sys.stderr`` is at that moment.

    Binding the stream once would keep writing to a stream that has since
    been replaced (by a process manager, a test harness or a redirect).
    """

    def __init__(self) -> None:
        super().__init__(sys.stderr)

    @property
    def stream(self) -> TextIO:
        return sys.stderr

    @stream.setter
    def stream(self, _value: TextIO) -> None:
        # Always the current sys.stderr; StreamHandler's own assignments
        # are ignored.
        pass


def is_default_audit_handler(handler: logging.Handler) -> bool:
    """Whether ``handler`` is the default handler installed here."""

    return getattr(handler, _DEFAULT_HANDLER_MARKER, False) is True


def _default_handler() -> logging.Handler:
    handler = _CurrentStderrHandler()
    handler.setFormatter(UtcIsoFormatter(AUDIT_LOG_FORMAT))
    setattr(handler, _DEFAULT_HANDLER_MARKER, True)

    return handler


def get_audit_logger() -> logging.Logger:
    """The logger administrative actions are recorded with."""

    return logging.getLogger(AUDIT_LOGGER_NAME)


def configure_audit_logging() -> logging.Logger:
    """Make sure INFO audit records are emitted somewhere; see module doc.

    Idempotent and thread-safe; never overrides operator configuration.
    """

    logger = get_audit_logger()

    with _configure_lock:
        logger.disabled = False

        if logger.level == logging.NOTSET:
            logger.setLevel(logging.INFO)

        if not logger.handlers:
            # Handler first: a record emitted in between may then reach
            # the root logger too, but is never lost.
            logger.addHandler(_default_handler())
            logger.propagate = False

    return logger


# ---------------------------------------------------------------------------
# Audit records
#
# Every record is one INFO line "<kind> key=value key=value ..." on the
# resolveai.audit logger, with the same fields attached to the record as
# ``record.<kind>`` (a dict) for structured handlers. Records are written
# only by the fixed-field helpers below, each called after its change has
# committed. Only IDs, fixed action/event names, allowlisted role and
# status values, booleans and counts are logged: never names, emails,
# passwords, hashes, tokens, ticket titles or descriptions, or request
# bodies.
# ---------------------------------------------------------------------------

ADMIN_ACTION = "admin_action"
TICKET_ACTION = "ticket_action"

AuditValue = str | int | bool | None

# Values containing anything else are quoted, so "In Progress" stays one
# key=value pair.
_BARE_VALUE = re.compile(r"[A-Za-z0-9_.:-]+")

_TICKET_STATUSES = frozenset({"Open", "In Progress", "Resolved"})
_ASSIGNMENT_EVENT_TYPES = frozenset({"assigned", "reassigned", "unassigned"})
_UNRECOGNISED = "unrecognised"


def _format_value(value: AuditValue) -> str:
    if value is None:
        return "none"

    text = f"{value}"

    return text if _BARE_VALUE.fullmatch(text) else f'"{text}"'


def _emit_audit_record(kind: str, fields: dict[str, AuditValue]) -> None:
    """Write one audit record. Private: callers use the helpers below."""

    # Idempotent and operator-respecting. Makes the audit log work in an
    # application that never imports app.main, and re-enables the logger
    # if a later logging configuration disabled it.
    audit_logger = configure_audit_logging()

    audit_logger.info(
        kind + " %s",
        " ".join(f"{key}={_format_value(value)}" for key, value in fields.items()),
        extra={kind: fields},
    )


def _id(value: object) -> int | None:
    """An integer ID; anything else is logged as none."""

    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _allowed(value: object, allowed: frozenset[str]) -> str:
    """An allowlisted value; anything else is logged as "unrecognised"."""

    return value if isinstance(value, str) and value in allowed else _UNRECOGNISED


def log_admin_action(
    action: str,
    *,
    actor_id: int,
    target_user_id: int,
    **values: str | bool | int,
) -> None:
    """Record an administrative action in the audit log (INFO).

    Called only after the action's transaction has committed. Only IDs,
    the action name, role/status values and counts (such as
    ``unassigned_tickets``) are logged; never names, emails, passwords,
    hashes, tokens, ticket content or request bodies. The same fields are
    attached to the record as ``record.admin_action`` for structured log
    handlers. (Also importable from ``app.api.routes.admin``.)
    """

    _emit_audit_record(
        ADMIN_ACTION,
        {
            "action": action,
            "actor_id": actor_id,
            "target_user_id": target_user_id,
            **values,
        },
    )


def log_ticket_assignment(
    *,
    actor_id: int,
    ticket_id: int,
    event_type: str,
    previous_assignee_id: int | None,
    new_assignee_id: int | None,
) -> None:
    """``ticket.assign``: an assignment, reassignment or unassignment.

    ``event_type`` is the history event (``assigned``, ``reassigned`` or
    ``unassigned``); ``new_assignee_id`` is none when unassigned.
    """

    _emit_audit_record(
        TICKET_ACTION,
        {
            "action": "ticket.assign",
            "actor_id": _id(actor_id),
            "ticket_id": _id(ticket_id),
            "event_type": _allowed(event_type, _ASSIGNMENT_EVENT_TYPES),
            "previous_assignee_id": _id(previous_assignee_id),
            "new_assignee_id": _id(new_assignee_id),
        },
    )


def log_ticket_claim(*, actor_id: int, ticket_id: int, assignee_id: int) -> None:
    """``ticket.claim``: an agent assigned an unassigned ticket to themselves."""

    _emit_audit_record(
        TICKET_ACTION,
        {
            "action": "ticket.claim",
            "actor_id": _id(actor_id),
            "ticket_id": _id(ticket_id),
            "assignee_id": _id(assignee_id),
        },
    )


def log_ticket_status_change(
    *,
    actor_id: int,
    ticket_id: int,
    status_before: str,
    status_after: str,
) -> None:
    """``ticket.status_change``: a ticket's status actually changed."""

    _emit_audit_record(
        TICKET_ACTION,
        {
            "action": "ticket.status_change",
            "actor_id": _id(actor_id),
            "ticket_id": _id(ticket_id),
            "status_before": _allowed(status_before, _TICKET_STATUSES),
            "status_after": _allowed(status_after, _TICKET_STATUSES),
        },
    )


def log_ticket_deletion(*, actor_id: int, ticket_id: int) -> None:
    """``ticket.delete``: a ticket (and, by cascade, its history) was deleted.

    The history rows are gone, so this record is the only one left.
    """

    _emit_audit_record(
        TICKET_ACTION,
        {
            "action": "ticket.delete",
            "actor_id": _id(actor_id),
            "ticket_id": _id(ticket_id),
        },
    )
