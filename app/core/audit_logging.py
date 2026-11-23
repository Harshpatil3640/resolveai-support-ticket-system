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

Callers must only log safe fields (IDs, action names, role and status
values); see ``app.api.routes.admin.log_admin_action``.
"""

import logging
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
