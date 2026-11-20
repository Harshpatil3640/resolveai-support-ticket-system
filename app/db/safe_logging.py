"""Keep database driver text out of SQLAlchemy's own log records.

SQLAlchemy's connection pool logs some failures itself, for example
"Exception during reset or similar" when a rollback fails while a
connection is returned to the pool. The driver exception is attached to
the record (``exc_info``) or passed as an argument, so any handler (Python's
last-resort handler, Uvicorn's, pytest's, an application's) would format
the driver's message and a traceback.

``DatabaseLogSanitiser`` is a ``logging.Filter`` attached to SQLAlchemy's
pool loggers. A logger-level filter runs before the record reaches ANY
handler, so every handler receives the sanitised record. Nothing else
changes: no handler is added, replaced, closed or removed; levels and
propagation are untouched; SQLAlchemy logging is not suppressed; records
without an exception pass through unchanged; and no extra records are
emitted, so nothing is duplicated.
"""

import logging
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager

import sqlalchemy.pool
from sqlalchemy.engine import Engine

from app.db.errors import describe_exception


SANITISED_MESSAGE = (
    "Database connection problem reported by SQLAlchemy (%s); details hidden."
)

# SQLAlchemy names each pool's logger after its class, e.g.
# "sqlalchemy.pool.impl.QueuePool". Every exception-bearing pool record is
# logged through one of these (sqlalchemy/pool/base.py, engine/base.py).
POOL_LOGGER_NAMES = tuple(
    f"{pool_class.__module__}.{pool_class.__name__}"
    for pool_class in (
        sqlalchemy.pool.QueuePool,
        sqlalchemy.pool.AsyncAdaptedQueuePool,
        sqlalchemy.pool.NullPool,
        sqlalchemy.pool.StaticPool,
        sqlalchemy.pool.SingletonThreadPool,
        sqlalchemy.pool.AssertionPool,
    )
)


def _sanitise_value(value: object) -> object:
    return describe_exception(value) if isinstance(value, BaseException) else value


class DatabaseLogSanitiser(logging.Filter):
    """Rewrite exception-bearing records using fixed text only."""

    def filter(self, record: logging.LogRecord) -> bool:
        exception = record.exc_info[1] if record.exc_info else None

        if exception is None and isinstance(record.msg, BaseException):
            exception = record.msg

        if exception is not None:
            record.msg = SANITISED_MESSAGE
            record.args = (describe_exception(exception),)
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
            return True

        # e.g. "Invalidate connection %r (reason: %s:%s)" passes the
        # exception itself as an argument.
        if isinstance(record.args, tuple):
            record.args = tuple(_sanitise_value(arg) for arg in record.args)
        elif isinstance(record.args, Mapping):
            record.args = {
                key: _sanitise_value(value)
                for key, value in record.args.items()
            }

        return True


_SANITISER = DatabaseLogSanitiser()


def _underlying_logger(logger: object) -> logging.Logger | None:
    """A pool's logger may be SQLAlchemy's InstanceLogger wrapper."""

    if isinstance(logger, logging.Logger):
        return logger

    wrapped = getattr(logger, "logger", None)

    return wrapped if isinstance(wrapped, logging.Logger) else None


def _pool_loggers(engines: Iterable[Engine]) -> list[logging.Logger]:
    loggers = {name: logging.getLogger(name) for name in POOL_LOGGER_NAMES}

    # Also cover a pool created with a logging_name (logger name suffix).
    for engine in engines:
        logger = _underlying_logger(engine.pool.logger)

        if logger is not None:
            loggers[logger.name] = logger

    return list(loggers.values())


@contextmanager
def sanitised_database_logs(*engines: Engine) -> Iterator[None]:
    """Sanitise SQLAlchemy pool log records while the block runs.

    Attaches the sanitiser to every pool logger that does not already have
    it and, on exit, removes it only from those loggers. Nested or
    repeated use (an API restart, a script inside a test client) is
    therefore safe and never duplicates the filter.
    """

    added: list[logging.Logger] = []

    for logger in _pool_loggers(engines):
        if _SANITISER not in logger.filters:
            logger.addFilter(_SANITISER)
            added.append(logger)

    try:
        yield
    finally:
        for logger in added:
            logger.removeFilter(_SANITISER)
