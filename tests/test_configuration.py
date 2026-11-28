"""Tests for the central configuration (Phase 11, step 1).

Settings are checked with ``load_settings``/``resolve_database_url`` on
explicit mappings, so no module is reloaded and the real environment is
never changed. Engines are created only for in-memory or ``tmp_path``
SQLite databases; Alembic runs in subprocesses against ``tmp_path`` files.
resolveai.db is never opened, and its hash is checked where a command
could otherwise reach it.
"""

import hashlib
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as real_create_engine
from sqlalchemy import event, text
from sqlalchemy.engine import URL, make_url

import app.db.database as app_database
from app.core import settings as settings_module
from app.core.settings import (
    DEFAULT_DATABASE_FILE,
    PROJECT_ROOT,
    ConfigurationError,
    Settings,
    load_settings,
    resolve_database_url,
)
from app.db import check_baseline
from app.db.database import (
    create_database_engine,
    enable_sqlite_foreign_keys,
    engine_options,
    sqlite_database_file,
)
from app.db.migrations import ALEMBIC_INI
from app.main import API_DOCS_URL, API_OPENAPI_URL, API_REDOC_URL, create_app


HEAD_REVISION = "0005"

# Test-only values; neither is a real secret.
VALID_SECRET = "test-only-secret-key-" + "x" * 32
DATABASE_PASSWORD = "db-password-must-not-leak"

APPLICATION_DATABASE = PROJECT_ROOT / "resolveai.db"

DOC_PATHS = (API_DOCS_URL, API_REDOC_URL, API_OPENAPI_URL)


def _environ(**values: str) -> dict[str, str]:
    """A minimal valid environment plus ``values``."""

    return {"SECRET_KEY": VALID_SECRET, **values}


def _sha256(path: Path) -> str | None:
    if not path.exists():
        return None

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _error_message(environ: dict[str, str]) -> str:
    with pytest.raises(ConfigurationError) as caught:
        load_settings(environ)

    return str(caught.value)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_defaults_are_compatible_with_development() -> None:
    settings = load_settings(_environ())

    assert settings.app_env == "development"
    assert settings.access_token_expire_minutes == 30
    assert settings.enable_api_docs is None
    assert settings.api_docs_enabled is True
    assert settings.log_format == "text"
    assert settings.login_rate_limit_max_attempts == 5
    assert settings.login_rate_limit_window_seconds == 300


def test_default_database_url_is_the_existing_sqlite_file() -> None:
    """Exactly the URL the application used before DATABASE_URL existed."""

    previous_default = URL.create(
        drivername="sqlite",
        database=str(PROJECT_ROOT / "resolveai.db"),
    )

    assert DEFAULT_DATABASE_FILE == PROJECT_ROOT / "resolveai.db"
    assert resolve_database_url({}) == previous_default
    assert load_settings(_environ()).database_url == previous_default


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blank_database_url_uses_the_default(blank: str) -> None:
    assert resolve_database_url({"DATABASE_URL": blank}) == (
        URL.create("sqlite", database=str(DEFAULT_DATABASE_FILE))
    )


@pytest.mark.skipif(
    "DATABASE_URL" in os.environ,
    reason="DATABASE_URL is configured for this test run.",
)
def test_application_uses_the_default_file_without_database_url() -> None:
    assert app_database.DATABASE_FILE == DEFAULT_DATABASE_FILE
    assert app_database.DATABASE_URL == resolve_database_url({})


# ---------------------------------------------------------------------------
# DATABASE_URL override
# ---------------------------------------------------------------------------


def test_database_url_override_with_absolute_sqlite_file(tmp_path: Path) -> None:
    database_file = tmp_path / "configured.db"
    configured = f"sqlite:///{database_file.as_posix()}"

    url = resolve_database_url({"DATABASE_URL": configured})

    assert url.get_backend_name() == "sqlite"
    assert Path(url.database) == database_file
    assert sqlite_database_file(url) == database_file
    assert not database_file.exists()  # resolving never creates the file


def test_relative_sqlite_path_is_resolved_against_the_project_root() -> None:
    url = resolve_database_url({"DATABASE_URL": "sqlite:///data/other.db"})

    assert Path(url.database) == PROJECT_ROOT / "data" / "other.db"


@pytest.mark.parametrize(
    "configured",
    ["sqlite://", "sqlite:///:memory:"],
)
def test_in_memory_sqlite_is_used_as_given(configured: str) -> None:
    url = resolve_database_url({"DATABASE_URL": configured})

    assert url == make_url(configured)
    assert sqlite_database_file(url) is None


def test_non_sqlite_url_is_accepted_but_has_no_database_file() -> None:
    configured = f"postgresql://resolveai:{DATABASE_PASSWORD}@localhost/app"

    url = resolve_database_url({"DATABASE_URL": configured})

    assert url.get_backend_name() == "postgresql"
    assert sqlite_database_file(url) is None
    assert DATABASE_PASSWORD not in repr(url)


@pytest.mark.parametrize(
    "configured",
    [
        f"not a url {DATABASE_PASSWORD}",
        f"://user:{DATABASE_PASSWORD}@host/db",
    ],
)
def test_invalid_database_url_is_rejected_without_echoing_it(
    configured: str,
) -> None:
    with pytest.raises(ConfigurationError) as caught:
        resolve_database_url({"DATABASE_URL": configured})

    assert "DATABASE_URL is not a valid" in str(caught.value)
    assert DATABASE_PASSWORD not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


def test_unknown_dialect_is_rejected_without_echoing_the_url() -> None:
    configured = f"nosuchdialect://user:{DATABASE_PASSWORD}@host/db"

    with pytest.raises(ConfigurationError) as caught:
        resolve_database_url({"DATABASE_URL": configured})

    assert "dialect" in str(caught.value)
    assert DATABASE_PASSWORD not in str(caught.value)
    assert caught.value.__cause__ is None


# ---------------------------------------------------------------------------
# SQLite-only engine settings
# ---------------------------------------------------------------------------


def test_sqlite_engine_options_include_check_same_thread() -> None:
    assert engine_options(make_url("sqlite://")) == {
        "hide_parameters": True,
        "connect_args": {"check_same_thread": False},
    }


@pytest.mark.parametrize(
    "configured",
    [
        "postgresql://user:pw@localhost/app",
        "postgresql+psycopg://user:pw@localhost/app",
        "mysql+pymysql://user:pw@localhost/app",
    ],
)
def test_non_sqlite_engine_options_have_no_sqlite_arguments(
    configured: str,
) -> None:
    assert engine_options(make_url(configured)) == {"hide_parameters": True}


def test_sqlite_engine_enforces_foreign_keys(tmp_path: Path) -> None:
    url = make_url(f"sqlite:///{(tmp_path / 'fk.db').as_posix()}")
    engine = create_database_engine(url)

    try:
        assert event.contains(engine, "connect", enable_sqlite_foreign_keys)
        assert engine.hide_parameters is True

        with engine.connect() as connection:
            assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1
    finally:
        engine.dispose()


def test_in_memory_sqlite_engine_enforces_foreign_keys() -> None:
    engine = create_database_engine(make_url("sqlite://"))

    try:
        with engine.connect() as connection:
            assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1
    finally:
        engine.dispose()


def test_non_sqlite_engine_gets_no_sqlite_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither connect_args nor the PRAGMA listener reach another dialect.

    PostgreSQL drivers are not installed, so ``create_engine`` is replaced
    with a recorder that returns a stand-in engine; the listener check is
    made on that stand-in.
    """

    calls: list[tuple[URL, dict]] = []
    stand_in = real_create_engine("sqlite://")

    def recording_create_engine(url, **kwargs):
        calls.append((url, kwargs))
        return stand_in

    monkeypatch.setattr(app_database, "create_engine", recording_create_engine)

    url = make_url("postgresql://user:pw@localhost/app")

    try:
        engine = create_database_engine(url)

        assert calls == [(url, {"hide_parameters": True})]
        assert engine is stand_in
        assert not event.contains(engine, "connect", enable_sqlite_foreign_keys)
    finally:
        stand_in.dispose()


def test_application_engine_keeps_hidden_parameters() -> None:
    assert app_database.engine.hide_parameters is True


# ---------------------------------------------------------------------------
# APP_ENV, ENABLE_API_DOCS, LOG_FORMAT
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("development", "development"),
        ("test", "test"),
        ("production", "production"),
        (" Production ", "production"),
        ("", "development"),
    ],
)
def test_app_env_accepts_documented_names(configured: str, expected: str) -> None:
    assert load_settings(_environ(APP_ENV=configured)).app_env == expected


@pytest.mark.parametrize("configured", ["prod", "staging", "dev", "PRODUCTIONX"])
def test_invalid_app_env_is_rejected(configured: str) -> None:
    message = _error_message(_environ(APP_ENV=configured))

    assert message == "APP_ENV must be one of: development, test, production."


@pytest.mark.parametrize(
    ("app_env", "override", "enabled"),
    [
        ("development", None, True),
        ("test", None, True),
        ("production", None, False),
        ("production", "true", True),
        ("production", "1", True),
        ("production", "YES", True),
        ("development", "false", False),
        ("test", "off", False),
        ("production", "0", False),
    ],
)
def test_api_docs_enabled_rules(
    app_env: str,
    override: str | None,
    enabled: bool,
) -> None:
    environ = _environ(APP_ENV=app_env)

    if override is not None:
        environ["ENABLE_API_DOCS"] = override

    assert load_settings(environ).api_docs_enabled is enabled


@pytest.mark.parametrize("configured", ["maybe", "enabled", "2", "truee"])
def test_invalid_enable_api_docs_is_rejected(configured: str) -> None:
    message = _error_message(_environ(ENABLE_API_DOCS=configured))

    assert message.startswith("ENABLE_API_DOCS must be true or false")
    assert configured not in message


@pytest.mark.parametrize(("configured", "expected"), [("json", "json"), ("TEXT", "text")])
def test_log_format_accepts_documented_values(configured: str, expected: str) -> None:
    assert load_settings(_environ(LOG_FORMAT=configured)).log_format == expected


def test_invalid_log_format_is_rejected() -> None:
    assert _error_message(_environ(LOG_FORMAT="xml")) == (
        "LOG_FORMAT must be one of: text, json."
    )


# ---------------------------------------------------------------------------
# Documentation endpoints of the real application
# ---------------------------------------------------------------------------


def _client(**values: str) -> TestClient:
    # Not entered as a context manager: the lifespan is not needed here.
    return TestClient(create_app(load_settings(_environ(**values))))


def test_production_disables_api_documentation_by_default() -> None:
    client = _client(APP_ENV="production")

    for path in DOC_PATHS:
        assert client.get(path).status_code == 404, path

    # API routes and the frontend are unchanged.
    assert client.get("/api/health").json() == {
        "status": "healthy",
        "application": "ResolveAI",
    }
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/").status_code == 200


@pytest.mark.parametrize("app_env", ["development", "test"])
def test_development_and_test_serve_api_documentation(app_env: str) -> None:
    client = _client(APP_ENV=app_env)

    for path in DOC_PATHS:
        assert client.get(path).status_code == 200, path


def test_enable_api_docs_true_overrides_production() -> None:
    client = _client(APP_ENV="production", ENABLE_API_DOCS="true")

    for path in DOC_PATHS:
        assert client.get(path).status_code == 200, path


def test_enable_api_docs_false_disables_development_docs() -> None:
    client = _client(APP_ENV="development", ENABLE_API_DOCS="false")

    for path in DOC_PATHS:
        assert client.get(path).status_code == 404, path


def test_api_routes_are_identical_with_and_without_documentation() -> None:
    """The OpenAPI schema can still be built in-process when not served."""

    development = create_app(load_settings(_environ()))
    production = create_app(load_settings(_environ(APP_ENV="production")))

    development_paths = development.openapi()["paths"]

    assert development_paths
    assert production.openapi()["paths"] == development_paths


@pytest.mark.skipif(
    any(name in os.environ for name in ("APP_ENV", "ENABLE_API_DOCS")),
    reason="Documentation settings are configured for this test run.",
)
def test_module_application_serves_documentation_in_development() -> None:
    from app.main import app

    assert (app.docs_url, app.redoc_url, app.openapi_url) == DOC_PATHS


# ---------------------------------------------------------------------------
# SECRET_KEY and integer validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("secret", [None, "", "   ", "x" * 31])
def test_missing_or_short_secret_key_is_rejected(secret: str | None) -> None:
    environ = {} if secret is None else {"SECRET_KEY": secret}

    message = _error_message(environ)

    assert message == (
        "SECRET_KEY is missing or too short. "
        "Configure it in the project .env file."
    )


def test_short_secret_key_is_not_echoed() -> None:
    short_secret = "short-secret-value-1234"

    assert short_secret not in _error_message({"SECRET_KEY": short_secret})


def test_secret_key_of_minimum_length_is_accepted() -> None:
    secret = "k" * 32

    assert load_settings({"SECRET_KEY": secret}).secret_key == secret


def test_configuration_error_is_a_runtime_error() -> None:
    assert issubclass(ConfigurationError, RuntimeError)


def test_settings_repr_never_contains_secrets() -> None:
    settings = load_settings(
        _environ(
            DATABASE_URL=f"postgresql://user:{DATABASE_PASSWORD}@host/db",
        )
    )

    assert VALID_SECRET not in repr(settings)
    assert DATABASE_PASSWORD not in repr(settings)


@pytest.mark.parametrize(
    ("configured", "expected"),
    [("45", 45), (" 60 ", 60), ("+15", 15), ("525600", 525_600)],
)
def test_access_token_expire_minutes_accepts_integers(
    configured: str,
    expected: int,
) -> None:
    settings = load_settings(_environ(ACCESS_TOKEN_EXPIRE_MINUTES=configured))

    assert settings.access_token_expire_minutes == expected


@pytest.mark.parametrize(
    ("configured", "message"),
    [
        ("abc", "ACCESS_TOKEN_EXPIRE_MINUTES must be an integer."),
        ("1.5", "ACCESS_TOKEN_EXPIRE_MINUTES must be an integer."),
        ("1_000", "ACCESS_TOKEN_EXPIRE_MINUTES must be an integer."),
        ("٣٠", "ACCESS_TOKEN_EXPIRE_MINUTES must be an integer."),
        ("0", "ACCESS_TOKEN_EXPIRE_MINUTES must be positive."),
        ("-5", "ACCESS_TOKEN_EXPIRE_MINUTES must be positive."),
        ("525601", "ACCESS_TOKEN_EXPIRE_MINUTES must be at most 525600."),
    ],
)
def test_invalid_access_token_expire_minutes_is_rejected(
    configured: str,
    message: str,
) -> None:
    assert _error_message(_environ(ACCESS_TOKEN_EXPIRE_MINUTES=configured)) == (
        message
    )


@pytest.mark.parametrize(
    ("name", "maximum"),
    [
        ("LOGIN_RATE_LIMIT_MAX_ATTEMPTS", 1_000),
        ("LOGIN_RATE_LIMIT_WINDOW_SECONDS", 86_400),
    ],
)
def test_rate_limit_settings_are_validated(name: str, maximum: int) -> None:
    attribute = name.lower()

    assert getattr(load_settings(_environ(**{name: "10"})), attribute) == 10
    assert getattr(load_settings(_environ(**{name: str(maximum)})), attribute) == (
        maximum
    )
    assert _error_message(_environ(**{name: "ten"})) == f"{name} must be an integer."
    assert _error_message(_environ(**{name: "0"})) == f"{name} must be positive."
    assert _error_message(_environ(**{name: str(maximum + 1)})) == (
        f"{name} must be at most {maximum}."
    )


def test_settings_are_immutable() -> None:
    settings = load_settings(_environ())

    with pytest.raises(AttributeError):
        settings.app_env = "production"  # type: ignore[misc]


def test_config_module_exports_validated_settings() -> None:
    from app.core import config

    assert isinstance(config.settings, Settings)
    assert config.SECRET_KEY == config.settings.secret_key
    assert config.ACCESS_TOKEN_EXPIRE_MINUTES == (
        config.settings.access_token_expire_minutes
    )
    assert config.ALGORITHM == "HS256"


def test_settings_module_reads_only_the_given_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "not-a-valid-environment")

    assert load_settings(_environ()).app_env == "development"
    assert settings_module.load_settings({"SECRET_KEY": VALID_SECRET}).app_env == (
        "development"
    )


# ---------------------------------------------------------------------------
# Database tools without a SQLite file
# ---------------------------------------------------------------------------


def test_check_baseline_requires_database_for_non_sqlite_configuration(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(check_baseline, "DATABASE_FILE", None)
    before = _sha256(APPLICATION_DATABASE)

    with pytest.raises(SystemExit) as caught:
        check_baseline.main([])

    assert caught.value.code == 2
    assert "pass --database PATH" in capsys.readouterr().err
    assert _sha256(APPLICATION_DATABASE) == before


# ---------------------------------------------------------------------------
# Application and Alembic resolve the same URL (subprocesses)
# ---------------------------------------------------------------------------


def _subprocess_environ(database_url: str) -> dict[str, str]:
    environ = dict(os.environ)
    environ["DATABASE_URL"] = database_url
    environ["PYTHONIOENCODING"] = "utf-8"

    return environ


def _run(args: list[str], database_url: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *args],
        cwd=PROJECT_ROOT,
        env=_subprocess_environ(database_url),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )


def _alembic(database_url: str, *args: str) -> subprocess.CompletedProcess:
    return _run(["-m", "alembic", "-c", str(ALEMBIC_INI), *args], database_url)


def _recorded_revisions(database_file: Path) -> list[str]:
    with closing(sqlite3.connect(database_file)) as connection:
        return [
            row[0]
            for row in connection.execute("SELECT version_num FROM alembic_version")
        ]


def test_alembic_and_application_resolve_the_same_database_url(
    tmp_path: Path,
) -> None:
    database_file = tmp_path / "configured.db"
    configured = f"sqlite:///{database_file.as_posix()}"
    before = _sha256(APPLICATION_DATABASE)

    application = _run(
        [
            "-c",
            "from app.db.database import DATABASE_FILE, engine; "
            "print(DATABASE_FILE); print(engine.url.database)",
        ],
        configured,
    )

    assert application.returncode == 0, application.stderr
    assert [Path(line) for line in application.stdout.split()] == [
        database_file,
        database_file,
    ]
    assert not database_file.exists()  # importing never opens the database

    upgrade = _alembic(configured, "upgrade", "head")

    assert upgrade.returncode == 0, upgrade.stderr
    assert _recorded_revisions(database_file) == [HEAD_REVISION]
    assert _sha256(APPLICATION_DATABASE) == before


def test_alembic_x_db_url_still_overrides_database_url(tmp_path: Path) -> None:
    configured_file = tmp_path / "configured.db"
    override_file = tmp_path / "override.db"
    before = _sha256(APPLICATION_DATABASE)

    upgrade = _alembic(
        f"sqlite:///{configured_file.as_posix()}",
        "-x",
        f"db_url=sqlite:///{override_file.as_posix()}",
        "upgrade",
        "head",
    )

    assert upgrade.returncode == 0, upgrade.stderr
    assert _recorded_revisions(override_file) == [HEAD_REVISION]
    assert not configured_file.exists()
    assert _sha256(APPLICATION_DATABASE) == before
