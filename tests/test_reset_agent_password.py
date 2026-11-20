"""Tests for the agent password-reset script.

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

from app.core.security import create_access_token, hash_password, verify_password
from app.db.migrations import BASELINE_REVISION, get_alembic_config, sqlite_url
from app.models import User
from app.scripts import reset_agent_password as reset_module
from app.services import user_service
from app.services.auth_service import get_active_user_from_token


OLD_PASSWORD = "Original-Agent-Password-1"
NEW_PASSWORD = "Replacement-Agent-Password-2"


class ScriptedPrompts:
    """Stand-in for input()/getpass() that records every prompt shown."""

    def __init__(self) -> None:
        self.inputs: list[str] = []
        self.passwords: list[str] = []
        self.asked: list[str] = []
        self.password_prompts: list[str] = []

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

        return self.passwords.pop(0)


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
    """Fail the test if anything migrates or stamps ``protected``."""

    def _guard(real_command):
        def _guarded(config, *args, **kwargs):
            url = config.get_main_option("sqlalchemy.url")

            if url is None or (
                Path(make_url(url).database).resolve() == protected.resolve()
            ):
                raise AssertionError("the script must not run migrations.")

            return real_command(config, *args, **kwargs)

        return _guarded

    for name in ("upgrade", "downgrade", "stamp"):
        monkeypatch.setattr(command, name, _guard(getattr(command, name)))


def _add_user(engine: Engine, *, email: str, role: str = "agent") -> int:
    with Session(engine) as db:
        user = User(
            name="Existing User",
            email=email,
            password_hash=hash_password(OLD_PASSWORD),
            role=role,
        )
        db.add(user)
        db.commit()

        return user.id


def _credentials(engine: Engine, user_id: int) -> tuple[str, int]:
    """(password_hash, token_version) as stored in the database."""

    with Session(engine) as db:
        user = db.get(User, user_id)

        return user.password_hash, user.token_version


def _token_is_accepted(engine: Engine, token: str) -> bool:
    with Session(engine) as db:
        return get_active_user_from_token(db, token) is not None


@pytest.fixture
def database_file(tmp_path: Path) -> Path:
    return tmp_path / "reset.db"


@pytest.fixture
def script_engine(
    database_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Engine]:
    engine = create_engine(sqlite_url(database_file))
    monkeypatch.setattr(reset_module, "engine", engine)

    yield engine

    engine.dispose()


@pytest.fixture
def prompts(monkeypatch: pytest.MonkeyPatch) -> ScriptedPrompts:
    scripted = ScriptedPrompts()

    monkeypatch.setattr("builtins.input", scripted.input)
    monkeypatch.setattr(reset_module, "getpass", scripted.getpass)

    return scripted


@pytest.fixture
def head_database(
    database_file: Path,
    script_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> Engine:
    _migrate(database_file, "head")
    _forbid_migrations(monkeypatch, database_file)

    return script_engine


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


def _driver_error(message: str, code: str) -> sqlite3.OperationalError:
    """A sqlite3 error carrying a structured code, as real ones do."""

    error = sqlite3.OperationalError(message)
    error.sqlite_errorname = code

    return error


def _assert_no_secrets(output: str, *hashes: str) -> None:
    assert OLD_PASSWORD not in output
    assert NEW_PASSWORD not in output
    assert "$argon2" not in output

    for password_hash in hashes:
        assert password_hash not in output


# ---------------------------------------------------------------------------
# Successful reset
# ---------------------------------------------------------------------------


def test_reset_changes_password_and_signs_out_old_sessions(
    head_database: Engine,
    prompts: ScriptedPrompts,
    capsys: pytest.CaptureFixture[str],
    recorded_hashes: list[str],
) -> None:
    agent_id = _add_user(head_database, email="agent@example.test")
    old_hash, _ = _credentials(head_database, agent_id)
    old_token = create_access_token(subject=str(agent_id), token_version=0)

    assert _token_is_accepted(head_database, old_token)

    prompts.inputs = ["agent@example.test"]
    prompts.passwords = [NEW_PASSWORD, NEW_PASSWORD]

    reset_module.reset_agent_password()

    captured = capsys.readouterr()
    new_hash, token_version = _credentials(head_database, agent_id)

    assert "Password successfully reset!" in captured.out
    assert "signed out" in captured.out

    assert verify_password(NEW_PASSWORD, new_hash)
    assert not verify_password(OLD_PASSWORD, new_hash)
    assert token_version == 1

    # Tokens issued before the reset are rejected; new ones work.
    assert not _token_is_accepted(head_database, old_token)

    new_token = create_access_token(subject=str(agent_id), token_version=1)

    assert _token_is_accepted(head_database, new_token)

    _assert_no_secrets(captured.out + captured.err, old_hash, *recorded_hashes)


def test_legacy_mixed_case_email_is_found(
    head_database: Engine,
    prompts: ScriptedPrompts,
    capsys: pytest.CaptureFixture[str],
) -> None:
    agent_id = _add_user(head_database, email="Legacy.Agent@Example.TEST")

    prompts.inputs = ["  legacy.agent@example.test "]
    prompts.passwords = [NEW_PASSWORD, NEW_PASSWORD]

    reset_module.reset_agent_password()

    assert "Password successfully reset!" in capsys.readouterr().out

    new_hash, token_version = _credentials(head_database, agent_id)

    assert verify_password(NEW_PASSWORD, new_hash)
    assert token_version == 1


# ---------------------------------------------------------------------------
# Refusals that never ask for a password
# ---------------------------------------------------------------------------


def test_missing_account_is_reported(
    head_database: Engine,
    prompts: ScriptedPrompts,
    capsys: pytest.CaptureFixture[str],
) -> None:
    agent_id = _add_user(head_database, email="agent@example.test")
    before = _credentials(head_database, agent_id)

    prompts.inputs = ["nobody@example.test"]

    reset_module.reset_agent_password()

    assert "No account found with this email." in capsys.readouterr().out
    assert prompts.password_prompts == []
    assert _credentials(head_database, agent_id) == before


@pytest.mark.parametrize("role", ["admin", "customer"])
def test_non_agent_accounts_cannot_be_reset(
    head_database: Engine,
    prompts: ScriptedPrompts,
    capsys: pytest.CaptureFixture[str],
    role: str,
) -> None:
    user_id = _add_user(head_database, email=f"{role}@example.test", role=role)
    before = _credentials(head_database, user_id)

    prompts.inputs = [f"{role}@example.test"]

    reset_module.reset_agent_password()

    output = capsys.readouterr().out

    assert "This account is not registered with the agent role." in output
    assert prompts.password_prompts == []
    assert _credentials(head_database, user_id) == before


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

    prompts.inputs = ["agent@example.test"]
    prompts.passwords = [NEW_PASSWORD, NEW_PASSWORD]

    with pytest.raises(SystemExit) as exit_info:
        reset_module.reset_agent_password()

    assert exit_info.value.code == 1
    assert "Cannot reset an agent password." in capsys.readouterr().err
    assert prompts.asked == []
    assert _sha256(database_file) == before


# ---------------------------------------------------------------------------
# Invalid new passwords
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("passwords", "expected_message"),
    [
        (["short", "short"], "Password must contain at least 12 characters."),
        (["p" * 129, "p" * 129], "Password must contain at most 128 characters."),
        ([NEW_PASSWORD, NEW_PASSWORD + "x"], "Passwords do not match."),
    ],
    ids=["too-short", "too-long", "mismatch"],
)
def test_invalid_new_password_changes_nothing(
    head_database: Engine,
    prompts: ScriptedPrompts,
    capsys: pytest.CaptureFixture[str],
    passwords: list[str],
    expected_message: str,
) -> None:
    agent_id = _add_user(head_database, email="agent@example.test")
    before = _credentials(head_database, agent_id)

    prompts.inputs = ["agent@example.test"]
    prompts.passwords = passwords

    reset_module.reset_agent_password()

    assert expected_message in capsys.readouterr().out
    assert _credentials(head_database, agent_id) == before


# ---------------------------------------------------------------------------
# Failed resets change nothing and print nothing sensitive
# ---------------------------------------------------------------------------


def test_token_invalidation_failure_keeps_old_password_and_sessions(
    head_database: Engine,
    prompts: ScriptedPrompts,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    recorded_hashes: list[str],
) -> None:
    agent_id = _add_user(head_database, email="agent@example.test")
    before = _credentials(head_database, agent_id)
    old_token = create_access_token(subject=str(agent_id), token_version=0)

    def _failing_invalidate(*_args, **_kwargs):
        # A realistic error whose string form contains the new hash as a
        # bound parameter, as SQLAlchemy errors do by default.
        raise OperationalError(
            "UPDATE users SET password_hash=? WHERE users.id = ?",
            (recorded_hashes[-1], agent_id),
            _driver_error("database is locked", "SQLITE_BUSY"),
        )

    monkeypatch.setattr(
        user_service,
        "invalidate_user_sessions",
        _failing_invalidate,
    )

    prompts.inputs = ["agent@example.test"]
    prompts.passwords = [NEW_PASSWORD, NEW_PASSWORD]

    with pytest.raises(SystemExit) as exit_info:
        reset_module.reset_agent_password()

    captured = capsys.readouterr()

    assert exit_info.value.code == 1
    assert "Password reset failed (OperationalError" in captured.err
    assert "the database is locked [SQLITE_BUSY]" in captured.err
    assert "left unchanged" in captured.err

    assert _credentials(head_database, agent_id) == before
    assert _token_is_accepted(head_database, old_token)

    _assert_no_secrets(captured.out + captured.err, before[0], *recorded_hashes)


def test_password_write_failure_never_increments_token_version(
    database_file: Path,
    head_database: Engine,
    prompts: ScriptedPrompts,
    capsys: pytest.CaptureFixture[str],
    recorded_hashes: list[str],
) -> None:
    agent_id = _add_user(head_database, email="agent@example.test")
    before = _credentials(head_database, agent_id)

    # A trigger makes the password UPDATE itself fail. Triggers are not
    # part of the compared schema, so the head check still passes.
    with closing(sqlite3.connect(database_file)) as connection:
        connection.execute(
            "CREATE TRIGGER block_password_update "
            "BEFORE UPDATE OF password_hash ON users "
            "BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END"
        )
        connection.commit()

    prompts.inputs = ["agent@example.test"]
    prompts.passwords = [NEW_PASSWORD, NEW_PASSWORD]

    with pytest.raises(SystemExit) as exit_info:
        reset_module.reset_agent_password()

    captured = capsys.readouterr()

    assert exit_info.value.code == 1
    assert "Password reset failed" in captured.err
    assert recorded_hashes, "the failure must happen after hashing"
    assert _credentials(head_database, agent_id) == before

    _assert_no_secrets(captured.out + captured.err, before[0], *recorded_hashes)


def test_ambiguous_case_insensitive_match_is_refused(
    head_database: Engine,
    prompts: ScriptedPrompts,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Two legacy rows differing only in case: refuse rather than guess."""

    first = _add_user(head_database, email="twin@example.test")
    second = _add_user(head_database, email="Twin@Example.test")
    before = (
        _credentials(head_database, first),
        _credentials(head_database, second),
    )

    prompts.inputs = ["twin@example.test"]

    reset_module.reset_agent_password()

    assert "More than one account matches" in capsys.readouterr().out
    assert prompts.password_prompts == []
    assert (
        _credentials(head_database, first),
        _credentials(head_database, second),
    ) == before


FAKE_HASH = "$argon2id$v=19$m=65536,t=3,p=4$ZmFrZS1zYWx0$ZmFrZS1oYXNoLXZhbHVl"
RAW_DRIVER_TEXT = "driver detail leaked"


def _assert_sanitised(stream: str) -> None:
    assert FAKE_HASH not in stream
    assert "$argon2" not in stream
    assert RAW_DRIVER_TEXT not in stream
    assert "SELECT" not in stream
    assert "UPDATE" not in stream
    assert "Traceback" not in stream


def test_hash_inside_driver_message_is_never_printed(
    head_database: Engine,
    prompts: ScriptedPrompts,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The driver's own message carries a hash; only fixed text is shown."""

    agent_id = _add_user(head_database, email="agent@example.test")
    before = _credentials(head_database, agent_id)

    def _failing_invalidate(*_args, **_kwargs):
        raise OperationalError(
            "UPDATE users SET token_version=(users.token_version + ?)",
            None,
            _driver_error(f"{RAW_DRIVER_TEXT}: {FAKE_HASH}", "SQLITE_BUSY"),
        )

    monkeypatch.setattr(
        user_service,
        "invalidate_user_sessions",
        _failing_invalidate,
    )

    prompts.inputs = ["agent@example.test"]
    prompts.passwords = [NEW_PASSWORD, NEW_PASSWORD]

    with pytest.raises(SystemExit) as exit_info:
        reset_module.reset_agent_password()

    captured = capsys.readouterr()

    assert exit_info.value.code == 1
    _assert_sanitised(captured.out)
    _assert_sanitised(captured.err)
    assert "(OperationalError: the database is locked [SQLITE_BUSY])" in captured.err
    assert _credentials(head_database, agent_id) == before


# ---------------------------------------------------------------------------
# Account lookup failure
# ---------------------------------------------------------------------------


def test_lookup_failure_is_reported_safely_without_password_prompt(
    head_database: Engine,
    prompts: ScriptedPrompts,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_id = _add_user(head_database, email="agent@example.test")
    before = _credentials(head_database, agent_id)
    rollbacks: list[None] = []

    class FailingLookupSession(Session):
        def scalars(self, *_args, **_kwargs):
            # Both the parameters and the driver message carry secrets.
            raise OperationalError(
                "SELECT users.id, users.password_hash FROM users "
                "WHERE lower(users.email) = ?",
                ("agent@example.test", FAKE_HASH),
                _driver_error(
                    f"{RAW_DRIVER_TEXT}: {FAKE_HASH}",
                    "SQLITE_IOERR_READ",
                ),
            )

        def rollback(self) -> None:
            rollbacks.append(None)
            super().rollback()

    monkeypatch.setattr(reset_module, "Session", FailingLookupSession)

    prompts.inputs = ["agent@example.test"]
    prompts.passwords = [NEW_PASSWORD, NEW_PASSWORD]

    with pytest.raises(SystemExit) as exit_info:
        reset_module.reset_agent_password()

    captured = capsys.readouterr()

    assert exit_info.value.code == 1
    assert "Could not look up the account because of a database error" in (
        captured.err
    )
    assert "(OperationalError: a disk I/O error occurred [SQLITE_IOERR_READ])" in (
        captured.err
    )
    _assert_sanitised(captured.out)
    _assert_sanitised(captured.err)

    assert prompts.password_prompts == []
    assert prompts.asked == ["Enter the agent's registered email: "]
    assert rollbacks == [None]
    assert _credentials(head_database, agent_id) == before


# ---------------------------------------------------------------------------
# A failing rollback never produces a traceback or raw text (A8)
# ---------------------------------------------------------------------------


def _secret_rollback_error() -> OperationalError:
    """A rollback error whose parameters and driver message carry secrets."""

    return OperationalError(
        "ROLLBACK",
        (FAKE_HASH,),
        _driver_error(f"{RAW_DRIVER_TEXT} on rollback: {FAKE_HASH}", "SQLITE_BUSY"),
    )


def test_lookup_failure_with_failing_rollback_is_reported_safely(
    head_database: Engine,
    prompts: ScriptedPrompts,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_id = _add_user(head_database, email="agent@example.test")
    before = _credentials(head_database, agent_id)

    class FailingSession(Session):
        def scalars(self, *_args, **_kwargs):
            raise OperationalError(
                "SELECT users.id FROM users WHERE lower(users.email) = ?",
                ("agent@example.test",),
                _driver_error(
                    f"{RAW_DRIVER_TEXT}: {FAKE_HASH}",
                    "SQLITE_IOERR_READ",
                ),
            )

        def rollback(self) -> None:
            raise _secret_rollback_error()

    monkeypatch.setattr(reset_module, "Session", FailingSession)

    prompts.inputs = ["agent@example.test"]
    prompts.passwords = [NEW_PASSWORD, NEW_PASSWORD]

    with pytest.raises(SystemExit) as exit_info:
        reset_module.reset_agent_password()

    captured = capsys.readouterr()

    assert exit_info.value.code == 1
    assert (
        "Could not look up the account because of a database error "
        "(OperationalError: a disk I/O error occurred [SQLITE_IOERR_READ]). "
        "The rollback also failed (OperationalError: the database is locked "
        "[SQLITE_BUSY])."
    ) in captured.err
    _assert_sanitised(captured.out)
    _assert_sanitised(captured.err)
    assert "ROLLBACK" not in captured.out + captured.err

    assert prompts.password_prompts == []
    assert _credentials(head_database, agent_id) == before


def test_reset_failure_with_failing_rollback_is_reported_safely(
    head_database: Engine,
    prompts: ScriptedPrompts,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reset_password()'s own rollback fails; nothing is committed."""

    agent_id = _add_user(head_database, email="agent@example.test")
    before = _credentials(head_database, agent_id)
    old_token = create_access_token(subject=str(agent_id), token_version=0)

    def _failing_invalidate(*_args, **_kwargs):
        raise OperationalError(
            "UPDATE users SET token_version=(users.token_version + ?)",
            (1,),
            _driver_error(
                f"{RAW_DRIVER_TEXT}: {FAKE_HASH}",
                "SQLITE_IOERR_WRITE",
            ),
        )

    class RollbackFailsSession(Session):
        def rollback(self) -> None:
            raise _secret_rollback_error()  # SQLITE_BUSY

    monkeypatch.setattr(
        user_service,
        "invalidate_user_sessions",
        _failing_invalidate,
    )
    monkeypatch.setattr(reset_module, "Session", RollbackFailsSession)

    prompts.inputs = ["agent@example.test"]
    prompts.passwords = [NEW_PASSWORD, NEW_PASSWORD]

    with pytest.raises(SystemExit) as exit_info:
        reset_module.reset_agent_password()

    captured = capsys.readouterr()

    assert exit_info.value.code == 1
    # The invalidation failure stays primary; the failed rollback is
    # reported once, as the secondary diagnostic.
    assert (
        "Password reset failed (OperationalError: a disk I/O error occurred "
        "[SQLITE_IOERR_WRITE]). The rollback also failed (OperationalError: "
        "the database is locked [SQLITE_BUSY]). The password and existing "
        "sessions were left unchanged."
    ) in captured.err
    assert captured.err.count("The rollback also failed") == 1
    _assert_sanitised(captured.out)
    _assert_sanitised(captured.err)
    assert "ROLLBACK" not in captured.out + captured.err

    # Nothing was committed: the session closed without a commit.
    assert _credentials(head_database, agent_id) == before
    assert _token_is_accepted(head_database, old_token)
