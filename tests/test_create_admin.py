"""Tests for the first-administrator bootstrap script.

Every test points the script at its own SQLite file under ``tmp_path`` by
replacing the module's engine; the application database (resolveai.db) is
never used. Should that replacement ever fail, the script would only run
its read-only migration check against resolveai.db before stopping.
"""

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.roles import UserRole
from app.core.security import hash_password, verify_password
from app.db.migrations import BASELINE_REVISION, get_alembic_config, sqlite_url
from app.main import app
from app.models import User
from app.scripts import create_admin as create_admin_module
from app.services import user_service


HEAD_REVISION = "0004"

ADMIN_PASSWORD = "Admin-Bootstrap-Passphrase-42"


class ScriptedPrompts:
    """Stand-in for input()/getpass() that records every prompt shown."""

    def __init__(self) -> None:
        self.inputs: list[str] = []
        self.passwords: list[str] = []
        self.asked: list[str] = []
        self.password_prompts: list[str] = []
        self.on_last_password = None

    def input(self, prompt: str = "") -> str:
        self.asked.append(prompt)

        if not self.inputs:
            raise AssertionError(f"Unexpected input prompt: {prompt!r}")

        return self.inputs.pop(0)

    def getpass(self, prompt: str = "") -> str:
        self.asked.append(prompt)
        self.password_prompts.append(prompt)

        if not self.passwords:
            raise AssertionError(f"Unexpected password prompt: {prompt!r}")

        answer = self.passwords.pop(0)

        if not self.passwords and self.on_last_password is not None:
            self.on_last_password()

        return answer


# ---------------------------------------------------------------------------
# Helpers and fixtures
# ---------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _migrate(path: Path, revision: str) -> None:
    command.upgrade(
        get_alembic_config(sqlite_url(path), configure_logging=False),
        revision,
    )


def _forbid_migrations(
    monkeypatch: pytest.MonkeyPatch,
    protected: Path,
) -> None:
    """Fail the test if anything migrates or stamps ``protected``.

    Commands against other files stay allowed: the head check migrates a
    throwaway reference database. A command with no explicit URL would
    target the application database, so it is forbidden too.
    """

    def _guard(real_command):
        def _guarded(config, *args, **kwargs):
            url = config.get_main_option("sqlalchemy.url")

            if url is None or (
                Path(make_url(url).database).resolve() == protected.resolve()
            ):
                raise AssertionError("create_admin must not run migrations.")

            return real_command(config, *args, **kwargs)

        return _guarded

    for name in ("upgrade", "downgrade", "stamp"):
        monkeypatch.setattr(command, name, _guard(getattr(command, name)))


def _users(engine: Engine) -> list[tuple]:
    """Every stored user row, for before/after comparisons."""

    with Session(engine) as db:
        return [
            (
                user.id,
                user.name,
                user.email,
                user.password_hash,
                user.role,
                user.is_active,
                user.token_version,
            )
            for user in db.scalars(select(User).order_by(User.id))
        ]


def _add_user(
    engine: Engine,
    *,
    email: str,
    role: str,
    is_active: bool = True,
) -> None:
    with Session(engine) as db:
        db.add(
            User(
                name="Existing User",
                email=email,
                password_hash=hash_password("Existing-Password-123"),
                role=role,
                is_active=is_active,
            )
        )
        db.commit()


@pytest.fixture
def database_file(tmp_path: Path) -> Path:
    return tmp_path / "bootstrap.db"


@pytest.fixture
def script_engine(
    database_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Engine]:
    """Point the script (its checks and its writes) at the temporary file."""

    engine = create_engine(sqlite_url(database_file))
    monkeypatch.setattr(create_admin_module, "engine", engine)

    yield engine

    engine.dispose()


@pytest.fixture
def prompts(monkeypatch: pytest.MonkeyPatch) -> ScriptedPrompts:
    scripted = ScriptedPrompts()

    monkeypatch.setattr("builtins.input", scripted.input)
    monkeypatch.setattr(create_admin_module, "getpass", scripted.getpass)

    return scripted


@pytest.fixture
def head_database(
    database_file: Path,
    script_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> Engine:
    """A database at head; migrating it again fails the test."""

    _migrate(database_file, "head")
    _forbid_migrations(monkeypatch, database_file)

    return script_engine


def _answer_valid_admin(prompts: ScriptedPrompts) -> None:
    prompts.inputs = ["  First Admin  ", "  Admin@Example.TEST "]
    prompts.passwords = [ADMIN_PASSWORD, ADMIN_PASSWORD]


# ---------------------------------------------------------------------------
# Successful bootstrap
# ---------------------------------------------------------------------------


def test_first_administrator_is_created(
    head_database: Engine,
    prompts: ScriptedPrompts,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _answer_valid_admin(prompts)

    assert create_admin_module.create_admin() == 0

    output = capsys.readouterr().out

    with Session(head_database) as db:
        admin = db.scalars(select(User)).one()

    assert admin.name == "First Admin"
    assert admin.email == "admin@example.test"
    assert admin.role == UserRole.ADMIN.value
    assert admin.is_active is True
    assert admin.token_version == 0
    assert verify_password(ADMIN_PASSWORD, admin.password_hash)

    assert "Administrator created successfully!" in output
    assert f"ID: {admin.id}" in output
    assert "Name: First Admin" in output
    assert "Email: admin@example.test" in output
    assert "Role: admin" in output


def test_first_admin_can_coexist_with_existing_agents(
    head_database: Engine,
    prompts: ScriptedPrompts,
) -> None:
    _add_user(head_database, email="agent@example.test", role="agent")
    _answer_valid_admin(prompts)

    assert create_admin_module.create_admin() == 0

    roles = sorted(row[4] for row in _users(head_database))

    assert roles == ["admin", "agent"]


def test_output_never_contains_password_or_hash(
    head_database: Engine,
    prompts: ScriptedPrompts,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _answer_valid_admin(prompts)

    assert create_admin_module.create_admin() == 0

    captured = capsys.readouterr()
    output = captured.out + captured.err
    password_hash = _users(head_database)[0][3]

    assert ADMIN_PASSWORD not in output
    assert password_hash not in output
    assert "$argon2" not in output


# ---------------------------------------------------------------------------
# An existing administrator disables the bootstrap
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("is_active", [True, False], ids=["active", "inactive"])
def test_existing_administrator_blocks_bootstrap_before_any_prompt(
    database_file: Path,
    head_database: Engine,
    prompts: ScriptedPrompts,
    capsys: pytest.CaptureFixture[str],
    is_active: bool,
) -> None:
    _add_user(
        head_database,
        email="existing.admin@example.test",
        role="admin",
        is_active=is_active,
    )
    before_rows = _users(head_database)
    before_hash = _sha256(database_file)

    # Answers are available, so only the admin check can stop the prompts.
    _answer_valid_admin(prompts)

    assert create_admin_module.create_admin() == 1

    assert "administrator account already exists" in capsys.readouterr().err
    assert prompts.asked == []
    assert prompts.password_prompts == []
    assert _users(head_database) == before_rows
    assert _sha256(database_file) == before_hash


def test_administrator_created_during_prompts_blocks_commit(
    head_database: Engine,
    prompts: ScriptedPrompts,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The in-transaction re-check catches an admin created concurrently."""

    _answer_valid_admin(prompts)

    # Another process creates an administrator while this one prompts.
    prompts.on_last_password = lambda: _add_user(
        head_database,
        email="concurrent.admin@example.test",
        role="admin",
    )

    assert create_admin_module.create_admin() == 1

    assert "administrator account already exists" in capsys.readouterr().err

    emails = [row[2] for row in _users(head_database)]

    assert emails == ["concurrent.admin@example.test"]


# ---------------------------------------------------------------------------
# Database not usable
# ---------------------------------------------------------------------------


def test_unmigrated_database_is_refused_before_any_prompt(
    database_file: Path,
    script_engine: Engine,
    prompts: ScriptedPrompts,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _migrate(database_file, BASELINE_REVISION)
    before = _sha256(database_file)
    _forbid_migrations(monkeypatch, database_file)
    _answer_valid_admin(prompts)

    assert create_admin_module.create_admin() == 1

    error = capsys.readouterr().err

    assert "Cannot create the first administrator." in error
    assert f"requires revision {HEAD_REVISION}" in error
    assert prompts.asked == []
    assert _sha256(database_file) == before


def test_schema_mismatch_is_refused_before_any_prompt(
    database_file: Path,
    head_database: Engine,
    prompts: ScriptedPrompts,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with closing(sqlite3.connect(database_file)) as connection:
        connection.execute("ALTER TABLE users DROP COLUMN token_version")
        connection.commit()

    before = _sha256(database_file)
    _answer_valid_admin(prompts)

    assert create_admin_module.create_admin() == 1

    error = capsys.readouterr().err

    assert "does not match the ResolveAI schema" in error
    assert "users: missing column token_version" in error
    assert prompts.asked == []
    assert _sha256(database_file) == before


def test_missing_database_is_refused_without_creating_it(
    database_file: Path,
    script_engine: Engine,
    prompts: ScriptedPrompts,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_migrations(monkeypatch, database_file)

    assert create_admin_module.create_admin() == 1

    assert "Database not found" in capsys.readouterr().err
    assert prompts.asked == []
    assert not database_file.exists()


# ---------------------------------------------------------------------------
# Input validation and duplicates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("passwords", "expected_message"),
    [
        (["short", "short"], "Password must contain at least 12 characters."),
        (
            [ADMIN_PASSWORD, ADMIN_PASSWORD + "x"],
            "Passwords do not match.",
        ),
    ],
    ids=["weak-password", "password-mismatch"],
)
def test_bad_password_is_rejected_and_nothing_is_written(
    database_file: Path,
    head_database: Engine,
    prompts: ScriptedPrompts,
    capsys: pytest.CaptureFixture[str],
    passwords: list[str],
    expected_message: str,
) -> None:
    _add_user(head_database, email="agent@example.test", role="agent")
    before_rows = _users(head_database)
    before_hash = _sha256(database_file)

    prompts.inputs = ["First Admin", "admin@example.test"]
    prompts.passwords = passwords

    assert create_admin_module.create_admin() == 1

    assert expected_message in capsys.readouterr().err
    assert _users(head_database) == before_rows
    assert _sha256(database_file) == before_hash


def test_invalid_email_is_rejected_before_password_prompt(
    head_database: Engine,
    prompts: ScriptedPrompts,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prompts.inputs = ["First Admin", "not-an-email"]

    assert create_admin_module.create_admin() == 1

    assert "Invalid name or email." in capsys.readouterr().err
    assert prompts.password_prompts == []
    assert _users(head_database) == []


def test_duplicate_email_is_rejected_and_records_stay_intact(
    database_file: Path,
    head_database: Engine,
    prompts: ScriptedPrompts,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # An existing agent already uses the address (stored in another case).
    _add_user(head_database, email="Admin@Example.test", role="agent")
    before_rows = _users(head_database)
    before_hash = _sha256(database_file)

    _answer_valid_admin(prompts)

    assert create_admin_module.create_admin() == 1

    assert "An account with this email already exists." in capsys.readouterr().err
    assert _users(head_database) == before_rows
    assert _sha256(database_file) == before_hash


# ---------------------------------------------------------------------------
# Not exposed over HTTP
# ---------------------------------------------------------------------------


def test_bootstrap_is_not_exposed_as_an_http_route() -> None:
    paths = {getattr(route, "path", "") for route in app.routes}

    assert not any("admin" in path or "/api/users" in path for path in paths)


# ---------------------------------------------------------------------------
# Unexpected database failures never reveal secrets
# ---------------------------------------------------------------------------


def _driver_error(message: str, code: str) -> sqlite3.OperationalError:
    """A sqlite3 error carrying a structured code, as real ones do."""

    error = sqlite3.OperationalError(message)
    error.sqlite_errorname = code

    return error


@pytest.fixture
def recorded_hashes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every hash the service computes, so tests can look for leaks."""

    hashes: list[str] = []
    real_hash = user_service.hash_password

    def _recording_hash(password: str) -> str:
        hashes.append(real_hash(password))
        return hashes[-1]

    monkeypatch.setattr(user_service, "hash_password", _recording_hash)

    return hashes


def _assert_no_secrets(output: str, hashes: list[str]) -> None:
    assert hashes, "the failure must happen after the password was hashed"
    assert ADMIN_PASSWORD not in output
    assert "$argon2" not in output

    for password_hash in hashes:
        assert password_hash not in output


def test_insert_failure_after_hashing_is_reported_safely(
    database_file: Path,
    head_database: Engine,
    prompts: ScriptedPrompts,
    capsys: pytest.CaptureFixture[str],
    recorded_hashes: list[str],
) -> None:
    # A trigger makes the INSERT fail after the password was hashed.
    # Triggers are not part of the compared schema, so the head check passes.
    with closing(sqlite3.connect(database_file)) as connection:
        connection.execute(
            "CREATE TRIGGER block_insert BEFORE INSERT ON users "
            "BEGIN SELECT RAISE(ABORT, 'simulated failure'); END"
        )
        connection.commit()

    _answer_valid_admin(prompts)

    assert create_admin_module.create_admin() == 1

    captured = capsys.readouterr()

    assert "Could not create the administrator because of a database error" in (
        captured.err
    )
    assert "IntegrityError" in captured.err
    assert "rolled back" in captured.err
    assert _users(head_database) == []

    _assert_no_secrets(captured.out + captured.err, recorded_hashes)


def test_lock_during_commit_is_rolled_back_and_reported_safely(
    head_database: Engine,
    prompts: ScriptedPrompts,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    recorded_hashes: list[str],
) -> None:
    """A concurrent bootstrap holding the lock past SQLite's timeout."""

    rollbacks: list[None] = []

    class LockedSession(Session):
        def commit(self) -> None:
            # Like a real SQLAlchemy error, its string form contains the
            # bound parameters, including the new password hash.
            raise OperationalError(
                "INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
                ("First Admin", "admin@example.test", recorded_hashes[-1]),
                _driver_error("database is locked", "SQLITE_BUSY"),
            )

        def rollback(self) -> None:
            rollbacks.append(None)
            super().rollback()

    monkeypatch.setattr(create_admin_module, "Session", LockedSession)

    _answer_valid_admin(prompts)

    assert create_admin_module.create_admin() == 1

    captured = capsys.readouterr()

    assert "the database is locked [SQLITE_BUSY]" in captured.err
    assert "no administrator was created" in captured.err
    assert rollbacks, "the transaction must be rolled back"
    assert _users(head_database) == []

    _assert_no_secrets(captured.out + captured.err, recorded_hashes)


FAKE_HASH = "$argon2id$v=19$m=65536,t=3,p=4$ZmFrZS1zYWx0$ZmFrZS1oYXNoLXZhbHVl"
RAW_DRIVER_TEXT = "driver detail leaked"


def test_hash_inside_driver_message_is_never_printed(
    head_database: Engine,
    prompts: ScriptedPrompts,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The driver's own message carries a hash; only fixed text is shown."""

    class FailingSession(Session):
        def commit(self) -> None:
            raise OperationalError(
                "INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
                None,
                _driver_error(f"{RAW_DRIVER_TEXT}: {FAKE_HASH}", "SQLITE_BUSY"),
            )

    monkeypatch.setattr(create_admin_module, "Session", FailingSession)
    _answer_valid_admin(prompts)

    assert create_admin_module.create_admin() == 1

    captured = capsys.readouterr()

    for stream in (captured.out, captured.err):
        assert FAKE_HASH not in stream
        assert "$argon2" not in stream
        assert RAW_DRIVER_TEXT not in stream
        assert "INSERT INTO" not in stream
        assert "Traceback" not in stream

    assert "(OperationalError: the database is locked [SQLITE_BUSY])" in captured.err
    assert _users(head_database) == []


# ---------------------------------------------------------------------------
# A failing rollback never produces a traceback or raw text (A8)
# ---------------------------------------------------------------------------


def _assert_rollback_failure_hidden(captured) -> None:
    for stream in (captured.out, captured.err):
        assert FAKE_HASH not in stream
        assert "$argon2" not in stream
        assert RAW_DRIVER_TEXT not in stream
        assert "INSERT INTO" not in stream
        assert "ROLLBACK" not in stream
        assert "Traceback" not in stream
        assert "Exception during reset" not in stream


POOL_SANITISED_LINE = (
    "Database connection problem reported by SQLAlchemy (a disk I/O error "
    "occurred [SQLITE_IOERR]); details hidden."
)


@pytest.mark.parametrize(
    ("failing_step", "expected_lines"),
    [
        pytest.param(
            "commit",
            [
                # SQLAlchemy itself attempts the rollback after the failed
                # commit (its sanitised pool record is checked below) and
                # invalidates the connection, so nothing is kept.
                "Could not create the administrator because of a database "
                "error (OperationalError: the database is locked "
                "[SQLITE_BUSY]).",
            ],
            id="commit-then-rollback-fail",
        ),
        pytest.param(
            "insert",
            [
                # After the failed INSERT, SQLAlchemy's own rollback fails
                # and SQLAlchemy raises THAT error. The original failure is
                # still reported as primary; the rollback as secondary.
                "Could not create the administrator because of a database "
                "error (IntegrityError: a database constraint was violated "
                "[SQLITE_CONSTRAINT_TRIGGER]). The rollback also failed "
                "(OperationalError: a disk I/O error occurred [SQLITE_IOERR]); "
                "nothing was committed, so no administrator was created.",
            ],
            id="insert-then-rollback-fail",
        ),
    ],
)
def test_driver_level_rollback_failure_is_reported_safely(
    database_file: Path,
    prompts: ScriptedPrompts,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    failing_step: str,
    expected_lines: list[str],
) -> None:
    """The real sqlite3 connection fails, and so does its rollback.

    This also exercises SQLAlchemy's connection pool, which logs its own
    "Exception during reset or similar" record (with the driver error
    attached) when a rollback fails. The record reaches whatever handlers
    exist (here pytest's capture) already sanitised.
    """

    _migrate(database_file, "head")
    _forbid_migrations(monkeypatch, database_file)

    if failing_step == "insert":
        with closing(sqlite3.connect(database_file)) as connection:
            connection.execute(
                "CREATE TRIGGER block_insert BEFORE INSERT ON users "
                "BEGIN SELECT RAISE(ABORT, 'simulated failure'); END"
            )
            connection.commit()

    class FailingDriverConnection(sqlite3.Connection):
        failing = False

        def commit(self) -> None:
            if type(self).failing:
                raise _driver_error(f"{RAW_DRIVER_TEXT}: {FAKE_HASH}", "SQLITE_BUSY")
            super().commit()

        def rollback(self) -> None:
            if type(self).failing:
                raise _driver_error(
                    f"{RAW_DRIVER_TEXT} on rollback: {FAKE_HASH}",
                    "SQLITE_IOERR",
                )
            super().rollback()

    engine = create_engine(
        sqlite_url(database_file),
        connect_args={"factory": FailingDriverConnection},
    )
    monkeypatch.setattr(create_admin_module, "engine", engine)

    _answer_valid_admin(prompts)

    def _start_failing() -> None:
        FailingDriverConnection.failing = True

    # Fail only once the prompts are done, i.e. around the INSERT's commit.
    prompts.on_last_password = _start_failing

    try:
        exit_code = create_admin_module.create_admin()
    finally:
        FailingDriverConnection.failing = False
        engine.dispose()

    captured = capsys.readouterr()

    assert exit_code == 1

    for line in expected_lines:
        assert line in captured.err

    _assert_rollback_failure_hidden(captured)

    # SQLAlchemy's pool record was delivered, sanitised, to the handlers.
    pool_records = [
        record
        for record in caplog.records
        if record.name.startswith("sqlalchemy.pool")
    ]

    assert [record.getMessage() for record in pool_records] == [
        POOL_SANITISED_LINE
    ]
    assert all(record.exc_info is None for record in pool_records)

    for text in (caplog.text, *(r.getMessage() for r in caplog.records)):
        assert FAKE_HASH not in text
        assert RAW_DRIVER_TEXT not in text
        assert "Traceback" not in text

    # Nothing was committed.
    check_engine = create_engine(sqlite_url(database_file))

    try:
        assert _users(check_engine) == []
    finally:
        check_engine.dispose()


def test_rollback_failure_in_duplicate_email_handler_is_reported_safely(
    head_database: Engine,
    prompts: ScriptedPrompts,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Previously this rollback could raise out of the handler."""

    _add_user(head_database, email="admin@example.test", role="agent")
    before = _users(head_database)

    class RollbackFailsSession(Session):
        def rollback(self) -> None:
            raise OperationalError(
                "ROLLBACK",
                (FAKE_HASH,),
                _driver_error(f"{RAW_DRIVER_TEXT}: {FAKE_HASH}", "SQLITE_BUSY"),
            )

    monkeypatch.setattr(create_admin_module, "Session", RollbackFailsSession)
    _answer_valid_admin(prompts)

    assert create_admin_module.create_admin() == 1

    captured = capsys.readouterr()

    assert (
        "An account with this email already exists. The rollback also "
        "failed (OperationalError: the database is locked [SQLITE_BUSY])."
    ) in captured.err
    _assert_rollback_failure_hidden(captured)
    assert _users(head_database) == before
