"""Tests for the interactive agent-provisioning script.

Every test points the script at its own SQLite file under ``tmp_path`` by
replacing the module's engine; the application database (resolveai.db) is
never used. Should that replacement ever fail, the script would only run
its read-only migration check against resolveai.db, never a write.
"""

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from app.core.security import hash_password, verify_password
from app.db.migrations import BASELINE_REVISION, get_alembic_config, sqlite_url
from app.models import User
from app.scripts import create_agent as create_agent_module


HEAD_REVISION = "0002"

AGENT_PASSWORD = "StrongPassword123!"


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


def _revisions(path: Path) -> list[str]:
    with closing(sqlite3.connect(path)) as connection:
        return [
            row[0]
            for row in connection.execute("SELECT version_num FROM alembic_version")
        ]


def _table_names(path: Path) -> set[str]:
    with closing(sqlite3.connect(path)) as connection:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }


def _migrate(path: Path, revision: str) -> None:
    command.upgrade(
        get_alembic_config(sqlite_url(path), configure_logging=False),
        revision,
    )


def _forbid_migrations(
    monkeypatch: pytest.MonkeyPatch,
    protected: Path,
) -> None:
    """From now on, fail the test if anything migrates or stamps ``protected``.

    Alembic commands against other files are still allowed: the head check
    builds its reference schema by migrating a throwaway temporary database.
    A command with no explicit URL would target the application database,
    so it is forbidden too.
    """

    def _guard(real_command):
        def _guarded(config, *args, **kwargs):
            url = config.get_main_option("sqlalchemy.url")

            if url is None or (
                Path(make_url(url).database).resolve() == protected.resolve()
            ):
                raise AssertionError("create_agent must not run migrations.")

            return real_command(config, *args, **kwargs)

        return _guarded

    for name in ("upgrade", "downgrade", "stamp"):
        monkeypatch.setattr(command, name, _guard(getattr(command, name)))


def _count_users(engine: Engine) -> int:
    with Session(engine) as db:
        return db.scalar(select(func.count()).select_from(User))


@pytest.fixture
def database_file(tmp_path: Path) -> Path:
    return tmp_path / "provisioning.db"


@pytest.fixture
def script_engine(
    database_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Engine]:
    """Point the script (its check and its writes) at the temporary file."""

    engine = create_engine(sqlite_url(database_file))
    monkeypatch.setattr(create_agent_module, "engine", engine)

    yield engine

    engine.dispose()


@pytest.fixture
def prompts(monkeypatch: pytest.MonkeyPatch) -> ScriptedPrompts:
    scripted = ScriptedPrompts()

    monkeypatch.setattr("builtins.input", scripted.input)
    monkeypatch.setattr(create_agent_module, "getpass", scripted.getpass)

    return scripted


@pytest.fixture
def head_database(
    database_file: Path,
    script_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> Engine:
    """A database already migrated to head; migrating again is forbidden."""

    _migrate(database_file, "head")
    _forbid_migrations(monkeypatch, database_file)

    return script_engine


def _run_and_expect_refusal(capsys: pytest.CaptureFixture[str]) -> str:
    with pytest.raises(SystemExit) as exit_info:
        create_agent_module.create_agent()

    assert exit_info.value.code == 1

    return capsys.readouterr().err


# ---------------------------------------------------------------------------
# Refusal when the database is not at head
# ---------------------------------------------------------------------------


def test_refuses_when_migrations_are_pending(
    database_file: Path,
    script_engine: Engine,
    prompts: ScriptedPrompts,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _migrate(database_file, BASELINE_REVISION)

    error = _run_and_expect_refusal(capsys)

    assert "Cannot create an agent." in error
    assert f"is at revision {BASELINE_REVISION}" in error
    assert f"requires revision {HEAD_REVISION}" in error
    assert "alembic upgrade head" in error
    assert "docs/database-migrations.md" in error


def test_refusal_happens_before_any_prompt(
    database_file: Path,
    script_engine: Engine,
    prompts: ScriptedPrompts,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _migrate(database_file, BASELINE_REVISION)

    # Answers are available, so only the migration check can stop the
    # script from asking for them.
    prompts.inputs = ["Agent", "agent@example.test"]
    prompts.passwords = [AGENT_PASSWORD, AGENT_PASSWORD]

    _run_and_expect_refusal(capsys)

    assert prompts.password_prompts == []
    assert prompts.asked == []


def test_refusal_does_not_modify_the_database(
    database_file: Path,
    script_engine: Engine,
    prompts: ScriptedPrompts,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _migrate(database_file, BASELINE_REVISION)
    before = _sha256(database_file)
    _forbid_migrations(monkeypatch, database_file)

    _run_and_expect_refusal(capsys)

    assert _sha256(database_file) == before
    assert _revisions(database_file) == [BASELINE_REVISION]


def test_refuses_database_not_managed_by_alembic(
    database_file: Path,
    script_engine: Engine,
    prompts: ScriptedPrompts,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with closing(sqlite3.connect(database_file)) as connection:
        connection.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
        connection.commit()

    before = _sha256(database_file)
    _forbid_migrations(monkeypatch, database_file)

    error = _run_and_expect_refusal(capsys)

    assert "no Alembic revision recorded" in error
    assert "python -m app.db.check_baseline --stamp" in error
    assert prompts.asked == []
    assert _sha256(database_file) == before
    assert _table_names(database_file) == {"users"}


def test_refuses_missing_database_without_creating_it(
    database_file: Path,
    script_engine: Engine,
    prompts: ScriptedPrompts,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_migrations(monkeypatch, database_file)

    error = _run_and_expect_refusal(capsys)

    assert "Database not found" in error
    assert "alembic upgrade head" in error
    assert prompts.asked == []
    assert not database_file.exists()


# ---------------------------------------------------------------------------
# Normal behaviour when the database is at head
# ---------------------------------------------------------------------------


def test_creates_agent_when_database_is_at_head(
    database_file: Path,
    head_database: Engine,
    prompts: ScriptedPrompts,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prompts.inputs = ["New Agent", "  New.Agent@Example.TEST "]
    prompts.passwords = [AGENT_PASSWORD, AGENT_PASSWORD]

    create_agent_module.create_agent()

    assert "Agent created successfully!" in capsys.readouterr().out
    assert len(prompts.password_prompts) == 2

    with Session(head_database) as db:
        agent = db.scalar(select(User))

    assert agent is not None
    assert agent.name == "New Agent"
    assert agent.email == "new.agent@example.test"
    assert agent.role == "agent"
    assert agent.is_active is True
    assert agent.token_version == 0
    assert verify_password(AGENT_PASSWORD, agent.password_hash)

    # Provisioning never changes the migration state.
    assert _revisions(database_file) == [HEAD_REVISION]


def test_duplicate_email_is_still_rejected(
    head_database: Engine,
    prompts: ScriptedPrompts,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with Session(head_database) as db:
        db.add(
            User(
                name="Existing",
                email="agent@example.test",
                password_hash=hash_password(AGENT_PASSWORD),
            )
        )
        db.commit()

    prompts.inputs = ["Another Agent", "agent@example.test"]
    prompts.passwords = [AGENT_PASSWORD, AGENT_PASSWORD]

    create_agent_module.create_agent()

    assert "An account with this email already exists." in capsys.readouterr().out
    assert _count_users(head_database) == 1


@pytest.mark.parametrize(
    ("passwords", "expected_message"),
    [
        (["short", "short"], "Password must contain at least 12 characters."),
        (
            [AGENT_PASSWORD, AGENT_PASSWORD + "x"],
            "Passwords do not match.",
        ),
    ],
)
def test_password_validation_is_unchanged(
    head_database: Engine,
    prompts: ScriptedPrompts,
    capsys: pytest.CaptureFixture[str],
    passwords: list[str],
    expected_message: str,
) -> None:
    prompts.inputs = ["Agent", "agent@example.test"]
    prompts.passwords = passwords

    create_agent_module.create_agent()

    assert expected_message in capsys.readouterr().out
    assert _count_users(head_database) == 0


def test_invalid_email_is_rejected_before_password_prompt(
    head_database: Engine,
    prompts: ScriptedPrompts,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prompts.inputs = ["Agent", "not-an-email"]

    create_agent_module.create_agent()

    assert "Invalid name or email." in capsys.readouterr().out
    assert prompts.password_prompts == []
    assert _count_users(head_database) == 0


# ---------------------------------------------------------------------------
# Refusal when the revision is right but the database is not usable
# ---------------------------------------------------------------------------


def test_refuses_foreign_database_at_head_revision_before_any_prompt(
    database_file: Path,
    script_engine: Engine,
    prompts: ScriptedPrompts,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-ResolveAI database that merely records '0002' is refused."""

    with closing(sqlite3.connect(database_file)) as connection:
        connection.execute("CREATE TABLE invoices (id INTEGER PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL, "
            "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
        )
        connection.execute(
            f"INSERT INTO alembic_version VALUES ('{HEAD_REVISION}')"
        )
        connection.commit()

    before = _sha256(database_file)
    _forbid_migrations(monkeypatch, database_file)

    prompts.inputs = ["Agent", "agent@example.test"]
    prompts.passwords = [AGENT_PASSWORD, AGENT_PASSWORD]

    error = _run_and_expect_refusal(capsys)

    assert "does not match the ResolveAI schema" in error
    assert "missing table: users" in error
    assert "unexpected table: invoices" in error
    assert prompts.asked == []
    assert _sha256(database_file) == before


def test_refuses_corrupt_migration_records_without_upgrade_advice(
    database_file: Path,
    script_engine: Engine,
    prompts: ScriptedPrompts,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _migrate(database_file, "head")

    # A second, different known revision: inconsistent for a linear history.
    with closing(sqlite3.connect(database_file)) as connection:
        connection.execute(
            f"INSERT INTO alembic_version VALUES ('{BASELINE_REVISION}')"
        )
        connection.commit()

    before = _sha256(database_file)
    _forbid_migrations(monkeypatch, database_file)

    error = _run_and_expect_refusal(capsys)

    assert "records several revisions" in error
    assert "Do not run 'alembic upgrade head'" in error
    assert prompts.asked == []
    assert _sha256(database_file) == before
