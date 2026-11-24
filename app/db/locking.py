"""Serialised write transactions shared by the application's services.

A service that must check state and then write, with no other protected
change able to interleave (for example "at least one active administrator
must remain"), starts its transaction with ``begin_serialised_change()``:

* SQLite ignores ``SELECT ... FOR UPDATE``, and the pysqlite driver runs
  SELECTs outside any transaction until the first write. The helper
  therefore issues ``BEGIN IMMEDIATE``, which takes SQLite's single write
  lock BEFORE anything is read. A concurrent change waits (up to the
  connection's busy timeout) until this transaction commits or rolls back;
  if the lock is not released in time it fails with "database is locked"
  and changes nothing.
* Other databases are left as they are: the caller's reads that follow
  must take row locks themselves (``SELECT ... FOR UPDATE``).

The helper never commits or rolls back: the caller owns the transaction
and must end it (commit on success, roll back on any failure).

This module imports nothing from the application, so the database layer
and every service can use it without circular imports.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


class TransactionAlreadyStartedError(RuntimeError):
    """The session already has a database write transaction in progress.

    A serialised change must start its own transaction (it takes SQLite's
    write lock before reading), so it refuses to run inside a transaction
    that has already written. This is a programming error, never a client
    error.

    Services may raise a subclass of this error (for example
    ``app.services.user_service.TransactionAlreadyStartedError``), so
    ``except TransactionAlreadyStartedError`` written against this class
    catches them too.
    """


def begin_serialised_change(db: "Session") -> None:
    """Start the transaction for a serialised change, write-locked.

    On SQLite this takes the database write lock (BEGIN IMMEDIATE) before
    anything is read. Other databases rely on the FOR UPDATE row locks
    taken by the reads that follow; nothing is executed here for them.

    The transaction is left open for the caller, who must commit or roll
    it back. On failure nothing has been written by this function, and
    the session's existing transaction (if any) is left untouched.

    Raises:
        TransactionAlreadyStartedError: the session's SQLite connection is
            already inside a transaction (something was written earlier),
            so BEGIN IMMEDIATE cannot be issued.
        sqlalchemy.exc.OperationalError: the lock was not released by a
            concurrent change within the busy timeout.
    """

    connection = db.connection()

    if connection.dialect.name != "sqlite":
        return

    if connection.connection.driver_connection.in_transaction:
        raise TransactionAlreadyStartedError(
            "A serialised change must start a new transaction."
        )

    connection.exec_driver_sql("BEGIN IMMEDIATE")
