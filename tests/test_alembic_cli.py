"""Integration tests for the direct Alembic command line (FIX 2).

Each CLI test runs Alembic's real CLI entry point (``alembic.config.main``)
in a separate Python process, against a temporary SQLite file passed with
``-x db_url=...``; resolveai.db is never used. A small bootstrap can make
the real sqlite3 driver fail on a chosen SQL statement, with a fake Argon2
hash and SQL inside the driver's own error message.

Programmatic tests call ``alembic.command`` in-process, also against
temporary SQLite files only.
"""

import hashlib
import io
import logging
import os
import sqlite3
import subprocess
import sys
import traceback
from contextlib import closing
from pathlib import Path

import pytest

from alembic import command

from app.db.check_baseline import EXIT_DATABASE_ERROR
from app.db.migrations import (
    ALEMBIC_INI,
    PROJECT_ROOT,
    MigrationRevisionError,
    get_alembic_config,
    sqlite_url,
)


HEAD_REVISION = "0005"

FAKE_HASH = "$argon2id$v=19$m=65536,t=3,p=4$ZmFrZS1zYWx0$ZmFrZS1oYXNoLXZhbHVl"
RAW_DRIVER_TEXT = "driver detail leaked"
DRIVER_SQL = f"INSERT INTO secrets VALUES ('{FAKE_HASH}')"

# A database-provided revision value: a fake Argon2 hash, an ANSI escape
# sequence, a newline and SQL-like text.
HOSTILE_REVISION = f"9999-HOSTILE-{FAKE_HASH}\x1b[31m\nDROP TABLE secrets"

SAFE_UNKNOWN_REVISION_MESSAGE = (
    "Alembic could not resolve a recorded database revision. "
    "Revision details are hidden."
)

# Alembic's CLI reports a CommandError as "FAILED: ..." and calls
# sys.exit(-1); an uncaught exception (with a traceback) exits with 1.
ALEMBIC_FAILURE_EXIT_CODE = 0xFFFFFFFF if os.name == "nt" else 0xFF

# None of these may appear when an unknown revision is reported or purged.
UNKNOWN_REVISION_LEAKS = (
    HOSTILE_REVISION,
    FAKE_HASH,
    "$argon2",
    "HOSTILE",
    "\x1b",
    "DROP TABLE",
    "Can't locate revision",
    "Traceback",
    "[SQL:",
    "[parameters:",
    "SELECT ",
    "INSERT INTO",
    "DELETE FROM",
)

BOOTSTRAP = f'''
import os
import sqlite3
import sys

FAIL_PATTERN = sys.argv[1]
# Optional: make only connections to this database file fail.
FAIL_ONLY_FOR = os.environ.get("FAIL_ONLY_FOR")
_real_connect = sqlite3.connect


class _FailingCursor(sqlite3.Cursor):
    def execute(self, sql, parameters=()):
        if FAIL_PATTERN != "-" and sql.lstrip().upper().startswith(FAIL_PATTERN):
            error = sqlite3.OperationalError(
                {RAW_DRIVER_TEXT!r} + ": " + {FAKE_HASH!r} + " while running "
                + {DRIVER_SQL!r}
            )
            error.sqlite_errorname = "SQLITE_IOERR_WRITE"
            raise error

        return super().execute(sql, parameters)


class _FailingConnection(sqlite3.Connection):
    def cursor(self, factory=_FailingCursor):
        return super().cursor(factory)


def _is_failure_target(args):
    if FAIL_ONLY_FOR is None:
        return True

    return (
        bool(args)
        and isinstance(args[0], str)
        and os.path.normcase(os.path.abspath(args[0]))
        == os.path.normcase(os.path.abspath(FAIL_ONLY_FOR))
    )


def _connect(*args, **kwargs):
    if _is_failure_target(args):
        kwargs["factory"] = _FailingConnection
    return _real_connect(*args, **kwargs)


# SQLAlchemy's pysqlite dialect connects through sqlite3.dbapi2.
sqlite3.connect = _connect
sqlite3.dbapi2.connect = _connect

if os.environ.get("BOOTSTRAP_ENTRY") == "check_baseline":
    sys.path.insert(0, os.getcwd())
    from app.db.check_baseline import main as check_baseline_main

    sys.exit(check_baseline_main(sys.argv[2:]))

from alembic.config import main

main(argv=sys.argv[2:], prog="alembic")
'''


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_bootstrap(
    tmp_path: Path,
    database_file: Path,
    arguments: list[str],
    fail_on: str,
    extra_env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    # Never target anything outside this test's temporary directory.
    assert database_file.resolve().is_relative_to(tmp_path.resolve())

    bootstrap = tmp_path / "alembic_bootstrap.py"
    bootstrap.write_text(BOOTSTRAP, encoding="utf-8")

    return subprocess.run(
        [sys.executable, str(bootstrap), fail_on, *arguments],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        env={
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            **extra_env,
        },
    )


def _alembic(
    tmp_path: Path,
    database_file: Path,
    *arguments: str,
    fail_on: str = "-",
) -> subprocess.CompletedProcess[str]:
    """Run ``alembic <arguments>`` against ``database_file`` only."""

    return _run_bootstrap(
        tmp_path,
        database_file,
        [
            "-c",
            str(ALEMBIC_INI),
            "-x",
            f"db_url={sqlite_url(database_file)}",
            *arguments,
        ],
        fail_on,
        {},
    )


def _check_baseline(
    tmp_path: Path,
    database_file: Path,
    *arguments: str,
    fail_on: str = "-",
) -> subprocess.CompletedProcess[str]:
    """Run ``python -m app.db.check_baseline --database <file> <arguments>``.

    Only connections to ``database_file`` fail, so the reference schema
    that check_baseline builds in its own temporary database is unaffected.
    """

    return _run_bootstrap(
        tmp_path,
        database_file,
        ["--database", str(database_file), *arguments],
        fail_on,
        {
            "BOOTSTRAP_ENTRY": "check_baseline",
            "FAIL_ONLY_FOR": str(database_file),
        },
    )


def _tables(database_file: Path) -> set[str]:
    if not database_file.exists():
        return set()

    with closing(sqlite3.connect(database_file)) as connection:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        }


def _revisions(database_file: Path) -> list[str]:
    with closing(sqlite3.connect(database_file)) as connection:
        return [
            row[0]
            for row in connection.execute("SELECT version_num FROM alembic_version")
        ]


def _sha256(database_file: Path) -> str:
    return hashlib.sha256(database_file.read_bytes()).hexdigest()


def _unrelated_content(database_file: Path) -> dict[str, object]:
    """Schema and rows of every table except alembic_version."""

    with closing(sqlite3.connect(database_file)) as connection:
        schema = sorted(
            connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' AND tbl_name != 'alembic_version'"
            )
        )
        rows = {
            name: sorted(connection.execute(f'SELECT * FROM "{name}"'))
            for kind, name, _sql in schema
            if kind == "table"
        }

    return {"schema": schema, "rows": rows}


def _create_database_with_unknown_revision(
    tmp_path: Path,
    filename: str,
    revisions: tuple[str, ...] = (HOSTILE_REVISION,),
) -> Path:
    """Create a database recording ``revisions``, plus an unrelated table.

    The unrelated ``secrets`` table (named in the hostile value's SQL-like
    text, and holding the fake hash) must survive every command untouched.
    """

    database_file = tmp_path / filename

    with closing(sqlite3.connect(database_file)) as connection:
        connection.execute(
            """
            CREATE TABLE alembic_version (
                version_num VARCHAR(32) NOT NULL,
                CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
            )
            """
        )
        connection.executemany(
            "INSERT INTO alembic_version (version_num) VALUES (?)",
            [(revision,) for revision in revisions],
        )
        connection.execute(
            "CREATE TABLE secrets (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO secrets (id, value) VALUES (1, ?)",
            (FAKE_HASH,),
        )
        connection.commit()

    return database_file


def _programmatic_config(database_file: Path):
    """In-process Alembic config whose command output can be inspected.

    ``Config.__init__`` binds ``stdout=sys.stdout`` when Alembic is
    imported, so ``print_stdout`` (used by ``current``) would bypass
    pytest's ``capsys``; a buffer is used instead.
    """

    config = get_alembic_config(sqlite_url(database_file), configure_logging=False)
    config.stdout = io.StringIO()

    return config


def _assert_no_unknown_revision_leak(output: str) -> None:
    for fragment in UNKNOWN_REVISION_LEAKS:
        assert fragment not in output, f"output exposed {fragment!r}"


def _assert_safe_database_failure(
    result: subprocess.CompletedProcess[str],
) -> str:
    """Assert that a SQLAlchemy failure is reported without sensitive data."""

    output = result.stdout + result.stderr

    assert result.returncode == ALEMBIC_FAILURE_EXIT_CODE, output
    assert (
        "FAILED: Database error during migration (OperationalError: a disk "
        "I/O error occurred [SQLITE_IOERR_WRITE])."
    ) in output

    for fragment in (
        FAKE_HASH,
        "$argon2",
        RAW_DRIVER_TEXT,
        DRIVER_SQL,
        "INSERT INTO",
        "CREATE TABLE",
        "[SQL:",
        "[parameters:",
        "Traceback",
    ):
        assert fragment not in output, f"output exposed {fragment!r}"

    return output


def _assert_safe_unknown_revision_failure(
    result: subprocess.CompletedProcess[str],
) -> str:
    """Assert that an unknown revision is replaced with fixed safe text."""

    output = result.stdout + result.stderr

    # Reported through Alembic's own "FAILED:" path, not as a crash.
    assert result.returncode == ALEMBIC_FAILURE_EXIT_CODE, output
    assert f"FAILED: {SAFE_UNKNOWN_REVISION_MESSAGE}" in output
    _assert_no_unknown_revision_leak(output)

    return output


# ---------------------------------------------------------------------------
# Database failures during direct commands
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fail_on",
    ["CREATE TABLE USERS", "INSERT INTO ALEMBIC_VERSION"],
    ids=["schema-ddl-fails", "version-insert-fails"],
)
def test_upgrade_database_failure_is_reported_safely(
    tmp_path: Path,
    fail_on: str,
) -> None:
    database_file = tmp_path / "upgrade.db"

    result = _alembic(tmp_path, database_file, "upgrade", "head", fail_on=fail_on)

    _assert_safe_database_failure(result)

    # The migration transaction was rolled back: nothing was created.
    assert _tables(database_file) == set()


def test_stamp_database_failure_is_reported_safely(tmp_path: Path) -> None:
    database_file = tmp_path / "stamp.db"

    result = _alembic(
        tmp_path,
        database_file,
        "stamp",
        "head",
        fail_on="INSERT INTO ALEMBIC_VERSION",
    )

    _assert_safe_database_failure(result)
    assert _tables(database_file) == set()


def test_check_baseline_stamp_reports_env_database_error_safely(
    tmp_path: Path,
) -> None:
    """``check_baseline --stamp`` shows env.py's safe error, not a crash.

    The stamp runs through the real ``alembic/env.py``, which raises
    ``MigrationDatabaseError``. (test_migrations.py covers the separate
    SQLAlchemyError branch with a patched ``command.stamp``.)
    """

    database_file = tmp_path / "legacy.db"

    # A pre-Alembic database: exactly the baseline schema, no version table.
    assert _alembic(tmp_path, database_file, "upgrade", "0001").returncode == 0

    with closing(sqlite3.connect(database_file)) as connection:
        connection.execute("DROP TABLE alembic_version")
        connection.commit()

    before = _sha256(database_file)

    result = _check_baseline(
        tmp_path,
        database_file,
        "--stamp",
        fail_on="INSERT INTO ALEMBIC_VERSION",
    )
    output = result.stdout + result.stderr

    assert result.returncode == EXIT_DATABASE_ERROR, output
    assert "Schema matches baseline revision 0001." in result.stdout
    assert (
        "Could not stamp the baseline revision. Database error during "
        "migration (OperationalError: a disk I/O error occurred "
        "[SQLITE_IOERR_WRITE])."
    ) in result.stderr
    assert "so no revision was recorded" in result.stderr

    for fragment in (
        FAKE_HASH,
        "$argon2",
        RAW_DRIVER_TEXT,
        DRIVER_SQL,
        "INSERT INTO",
        "[SQL:",
        "[parameters:",
        "Traceback",
    ):
        assert fragment not in output, f"output exposed {fragment!r}"

    # The stamp's transaction was rolled back: nothing was recorded.
    assert "alembic_version" not in _tables(database_file)
    assert _sha256(database_file) == before


# ---------------------------------------------------------------------------
# Unknown revision values must never be shown
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "arguments",
    [
        ("upgrade", "head"),
        ("current",),
        ("stamp", "head"),
    ],
    ids=[
        "upgrade-unknown-revision",
        "current-unknown-revision",
        "stamp-unknown-revision",
    ],
)
def test_unknown_revision_commands_fail_safely(
    tmp_path: Path,
    arguments: tuple[str, ...],
) -> None:
    database_file = _create_database_with_unknown_revision(
        tmp_path,
        "unknown_revision.db",
    )
    before = _sha256(database_file)
    unrelated_before = _unrelated_content(database_file)

    result = _alembic(tmp_path, database_file, *arguments)

    _assert_safe_unknown_revision_failure(result)

    # A failed command changes nothing: the hostile row is still the only
    # recorded revision, the unrelated table is intact, and the file is
    # byte-for-byte identical.
    assert _revisions(database_file) == [HOSTILE_REVISION]
    assert _unrelated_content(database_file) == unrelated_before
    assert _sha256(database_file) == before


@pytest.mark.parametrize(
    "recorded",
    [
        (HOSTILE_REVISION,),
        ("0001", HOSTILE_REVISION),
        (f"0002@{FAKE_HASH}\x1b[0m",),
    ],
    ids=["hostile-only", "known-plus-hostile", "branch-syntax"],
)
@pytest.mark.parametrize(
    "run",
    [
        lambda config: command.upgrade(config, "head"),
        lambda config: command.current(config),
        lambda config: command.stamp(config, "head"),
    ],
    ids=["upgrade", "current", "stamp"],
)
def test_programmatic_unknown_revision_error_has_no_exception_chain(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    recorded: tuple[str, ...],
    run,
) -> None:
    """The raised error, its chain and any formatted traceback are safe."""

    caplog.set_level(logging.INFO, logger="alembic")

    database_file = _create_database_with_unknown_revision(
        tmp_path,
        "programmatic_unknown.db",
        recorded,
    )
    before = _sha256(database_file)

    config = _programmatic_config(database_file)

    with pytest.raises(MigrationRevisionError) as error_info:
        run(config)

    error = error_info.value

    assert str(error).startswith(SAFE_UNKNOWN_REVISION_MESSAGE)

    # Not chained, explicitly or implicitly, to Alembic's original error.
    assert error.__cause__ is None
    assert error.__context__ is None

    formatted = "".join(traceback.format_exception(error))
    captured = capsys.readouterr()
    command_output = config.stdout.getvalue()

    for text in (formatted, command_output, captured.out, captured.err, caplog.text):
        for fragment in (*recorded, FAKE_HASH, "$argon2", "\x1b", "HOSTILE"):
            assert fragment not in text, f"exposed {fragment!r}"
        assert "Can't locate revision" not in text

    assert _sha256(database_file) == before


def test_mistyped_destination_keeps_alembics_own_message(
    tmp_path: Path,
) -> None:
    """Only database-provided values are hidden, not the operator's input.

    When every recorded revision is one of this code's own revisions, the
    unresolvable text came from the command line, so Alembic's usual
    message is kept.
    """

    database_file = tmp_path / "healthy.db"

    assert _alembic(tmp_path, database_file, "upgrade", "head").returncode == 0

    # A revision ID that does not exist in alembic/versions.
    result = _alembic(tmp_path, database_file, "upgrade", "0099")
    output = result.stdout + result.stderr

    assert result.returncode == ALEMBIC_FAILURE_EXIT_CODE, output
    assert "FAILED: Can't locate revision identified by '0099'" in output
    assert SAFE_UNKNOWN_REVISION_MESSAGE not in output
    assert "Traceback" not in output
    assert _revisions(database_file) == [HEAD_REVISION]


# ---------------------------------------------------------------------------
# Purge recovery must continue to work
# ---------------------------------------------------------------------------


def test_cli_stamp_purge_recovers_from_unknown_revision(tmp_path: Path) -> None:
    database_file = _create_database_with_unknown_revision(
        tmp_path,
        "cli_purge.db",
    )
    unrelated_before = _unrelated_content(database_file)

    result = _alembic(tmp_path, database_file, "stamp", "--purge", "head")
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    _assert_no_unknown_revision_leak(output)

    # The hostile row was replaced by exactly the head revision; nothing
    # else in the database changed (stamp runs no migrations).
    assert _revisions(database_file) == [HEAD_REVISION]
    assert _tables(database_file) == {"alembic_version", "secrets"}
    assert _unrelated_content(database_file) == unrelated_before

    # Alembic now resolves the recorded revision normally.
    current = _alembic(tmp_path, database_file, "current")

    assert current.returncode == 0, current.stdout + current.stderr
    assert f"{HEAD_REVISION} (head)" in current.stdout


def test_programmatic_stamp_purge_recovers_from_unknown_revision(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="alembic")

    database_file = _create_database_with_unknown_revision(
        tmp_path,
        "programmatic_purge.db",
    )
    unrelated_before = _unrelated_content(database_file)

    config = _programmatic_config(database_file)

    # Programmatic equivalent of: alembic stamp --purge head
    command.stamp(config, "head", purge=True)

    captured = capsys.readouterr()

    assert _revisions(database_file) == [HEAD_REVISION]
    assert _tables(database_file) == {"alembic_version", "secrets"}
    assert _unrelated_content(database_file) == unrelated_before

    _assert_no_unknown_revision_leak(
        config.stdout.getvalue() + captured.out + captured.err + caplog.text
    )

    # Alembic now resolves the recorded revision normally.
    command.current(config)

    assert f"{HEAD_REVISION} (head)" in config.stdout.getvalue()


# ---------------------------------------------------------------------------
# Normal commands still work
# ---------------------------------------------------------------------------


def test_upgrade_downgrade_and_stamp_still_work(tmp_path: Path) -> None:
    database_file = tmp_path / "normal.db"

    upgrade = _alembic(tmp_path, database_file, "upgrade", "head")

    assert upgrade.returncode == 0, upgrade.stdout + upgrade.stderr
    assert _tables(database_file) == {
        "alembic_version",
        "users",
        "tickets",
        "ticket_events",
        "ticket_messages",
        "notifications",
        "revoked_tokens",
    }
    assert _revisions(database_file) == [HEAD_REVISION]

    downgrade = _alembic(tmp_path, database_file, "downgrade", "0001")

    assert downgrade.returncode == 0, downgrade.stdout + downgrade.stderr
    assert _revisions(database_file) == ["0001"]

    stamp_file = tmp_path / "stamped.db"
    stamp = _alembic(tmp_path, stamp_file, "stamp", "head")

    assert stamp.returncode == 0, stamp.stdout + stamp.stderr
    assert _revisions(stamp_file) == [HEAD_REVISION]


# ---------------------------------------------------------------------------
# The baseline refusal through the CLI (FIX 1)
# ---------------------------------------------------------------------------


def test_baseline_refusal_via_cli_never_names_tables(tmp_path: Path) -> None:
    database_file = tmp_path / "occupied.db"
    hostile_name = f"leak_{FAKE_HASH}\x1b[31m"

    with closing(sqlite3.connect(database_file)) as connection:
        connection.execute(f'CREATE TABLE "{hostile_name}" (id INTEGER PRIMARY KEY)')
        connection.commit()

    result = _alembic(tmp_path, database_file, "upgrade", "head")
    output = result.stdout + result.stderr

    assert result.returncode != 0
    assert "already contains 1 table (names not shown)" in output

    for fragment in (FAKE_HASH, "$argon2", "\x1b", "leak_"):
        assert fragment not in output

    # Refused before anything was created; the table is still there.
    assert _tables(database_file) == {hostile_name}
