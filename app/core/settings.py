"""Central application settings, read from environment variables.

Every setting ResolveAI reads from the environment is parsed and validated
here. The functions are pure (they read only the mapping they are given),
so tests can check any combination without reloading modules:

* ``load_settings(environ)`` validates everything and returns ``Settings``.
  ``app.core.config`` calls it at import time with ``os.environ``, so the
  application refuses to start with an invalid configuration.
* ``resolve_database_url(environ)`` resolves only ``DATABASE_URL``. The
  database layer (``app.db.database``), and through it Alembic, use it
  directly, so running migrations does not require ``SECRET_KEY``.

Variables (all optional except ``SECRET_KEY``; see ``.env.example``):

=================================== =========================================
``SECRET_KEY``                      JWT signing key, at least 32 characters.
``ACCESS_TOKEN_EXPIRE_MINUTES``     Integer 1..525600 (one year). Default 30.
``DATABASE_URL``                    SQLAlchemy URL. Default: the SQLite file
                                    ``resolveai.db`` in the project root.
``APP_ENV``                         ``development`` (default), ``test`` or
                                    ``production``.
``ENABLE_API_DOCS``                 ``true``/``false`` (also 1/0, yes/no,
                                    on/off). Default: enabled except in
                                    production.
``LOG_FORMAT``                      ``text`` (default) or ``json``.
``LOGIN_RATE_LIMIT_MAX_ATTEMPTS``   Integer 1..1000. Default 5.
``LOGIN_RATE_LIMIT_WINDOW_SECONDS`` Integer 1..86400. Default 300.
=================================== =========================================

A variable that is unset, empty or only whitespace uses its default (a
missing ``SECRET_KEY`` is still an error). ``SECRET_KEY`` and
``DATABASE_URL`` are used exactly as given; ``APP_ENV``, ``LOG_FORMAT``
and ``ENABLE_API_DOCS`` ignore case and surrounding whitespace.

Error messages name the variable and the accepted values, never the value
that was supplied: a misplaced secret or a database password must not end
up in a log or a traceback. Errors raised while parsing ``DATABASE_URL``
are not chained, because SQLAlchemy's own messages quote the URL.

``LOG_FORMAT`` and the login rate-limit settings are validated now but not
yet used by the application.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, NoSuchModuleError


# Locate the project root directory.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Optional local configuration file (git-ignored).
ENV_FILE = PROJECT_ROOT / ".env"

# The application database when DATABASE_URL is not set.
DEFAULT_DATABASE_FILE = PROJECT_ROOT / "resolveai.db"


class ConfigurationError(RuntimeError):
    """The environment configuration is missing or invalid.

    A ``RuntimeError`` subclass, so code that caught the errors raised by
    the previous configuration module keeps working.
    """


# ---------------------------------------------------------------------------
# Accepted values
# ---------------------------------------------------------------------------

DEVELOPMENT = "development"
TEST = "test"
PRODUCTION = "production"

APP_ENVIRONMENTS = (DEVELOPMENT, TEST, PRODUCTION)

LOG_FORMAT_TEXT = "text"
LOG_FORMAT_JSON = "json"

LOG_FORMATS = (LOG_FORMAT_TEXT, LOG_FORMAT_JSON)

MIN_SECRET_KEY_LENGTH = 32

DEFAULT_ACCESS_TOKEN_EXPIRE_MINUTES = 30
MAX_ACCESS_TOKEN_EXPIRE_MINUTES = 525_600  # one year

DEFAULT_LOGIN_RATE_LIMIT_MAX_ATTEMPTS = 5
MAX_LOGIN_RATE_LIMIT_MAX_ATTEMPTS = 1_000

DEFAULT_LOGIN_RATE_LIMIT_WINDOW_SECONDS = 300
MAX_LOGIN_RATE_LIMIT_WINDOW_SECONDS = 86_400  # one day

_TRUE_VALUES = frozenset({"true", "1", "yes", "on"})
_FALSE_VALUES = frozenset({"false", "0", "no", "off"})

# ASCII digits only: int() would also accept "1_000" and non-ASCII digits.
_INTEGER = re.compile(r"[+-]?[0-9]+")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    """Validated application settings (immutable)."""

    # Never shown in repr(), so printing or logging Settings is safe.
    secret_key: str = field(repr=False)
    access_token_expire_minutes: int
    # SQLAlchemy masks the password in repr(URL).
    database_url: URL
    app_env: str
    # None: not configured, use the environment's default.
    enable_api_docs: bool | None
    log_format: str
    login_rate_limit_max_attempts: int
    login_rate_limit_window_seconds: int

    @property
    def is_production(self) -> bool:
        return self.app_env == PRODUCTION

    @property
    def api_docs_enabled(self) -> bool:
        """Whether /docs, /redoc and /openapi.json are served.

        ``ENABLE_API_DOCS`` decides when set; otherwise the documentation
        is served in development and test and disabled in production.
        """

        if self.enable_api_docs is not None:
            return self.enable_api_docs

        return not self.is_production


def load_environment() -> None:
    """Load the project's ``.env`` file into ``os.environ``, if present.

    Variables already set in the real environment take precedence (they
    are never overridden), so a deployment or CI can configure everything
    without a ``.env`` file. Idempotent.
    """

    load_dotenv(ENV_FILE)


def load_settings(environ: Mapping[str, str]) -> Settings:
    """Validate every setting in ``environ``.

    Raises:
        ConfigurationError: a setting is missing or invalid. The message
            names the variable, never its value.
    """

    return Settings(
        secret_key=_read_secret_key(environ),
        access_token_expire_minutes=_read_int(
            environ,
            "ACCESS_TOKEN_EXPIRE_MINUTES",
            default=DEFAULT_ACCESS_TOKEN_EXPIRE_MINUTES,
            maximum=MAX_ACCESS_TOKEN_EXPIRE_MINUTES,
        ),
        database_url=resolve_database_url(environ),
        app_env=_read_choice(
            environ,
            "APP_ENV",
            APP_ENVIRONMENTS,
            default=DEVELOPMENT,
        ),
        enable_api_docs=_read_bool(environ, "ENABLE_API_DOCS"),
        log_format=_read_choice(
            environ,
            "LOG_FORMAT",
            LOG_FORMATS,
            default=LOG_FORMAT_TEXT,
        ),
        login_rate_limit_max_attempts=_read_int(
            environ,
            "LOGIN_RATE_LIMIT_MAX_ATTEMPTS",
            default=DEFAULT_LOGIN_RATE_LIMIT_MAX_ATTEMPTS,
            maximum=MAX_LOGIN_RATE_LIMIT_MAX_ATTEMPTS,
        ),
        login_rate_limit_window_seconds=_read_int(
            environ,
            "LOGIN_RATE_LIMIT_WINDOW_SECONDS",
            default=DEFAULT_LOGIN_RATE_LIMIT_WINDOW_SECONDS,
            maximum=MAX_LOGIN_RATE_LIMIT_WINDOW_SECONDS,
        ),
    )


# ---------------------------------------------------------------------------
# Database URL
# ---------------------------------------------------------------------------


def default_database_url() -> URL:
    """The SQLite URL of ``resolveai.db`` in the project root."""

    return URL.create(
        drivername="sqlite",
        database=str(DEFAULT_DATABASE_FILE),
    )


def is_sqlite_url(url: URL) -> bool:
    """Whether ``url`` uses the SQLite dialect (with any driver)."""

    return url.get_backend_name() == "sqlite"


def resolve_database_url(environ: Mapping[str, str]) -> URL:
    """Return the configured database URL, or the default SQLite file.

    A relative SQLite file path (``sqlite:///data/app.db``) is resolved
    against the project root, so the application and Alembic open the
    same file whatever directory they are started from. Absolute paths,
    in-memory databases and ``file:`` URIs are used as given.

    Other dialects are accepted when SQLAlchemy knows them, but only
    SQLite is currently supported by ResolveAI; their database drivers
    are not installed.

    Raises:
        ConfigurationError: the value is not a valid SQLAlchemy URL, or
            names a dialect SQLAlchemy does not know. The URL (which may
            contain a password) is not included or chained.
    """

    raw = _read_optional(environ, "DATABASE_URL")

    if raw is None:
        return default_database_url()

    try:
        url = make_url(raw.strip())
    except (ArgumentError, ValueError):
        raise ConfigurationError(
            "DATABASE_URL is not a valid SQLAlchemy database URL "
            "(for example sqlite:///C:/path/to/resolveai.db)."
        ) from None

    try:
        url.get_dialect()
    except NoSuchModuleError:
        raise ConfigurationError(
            "DATABASE_URL names a database dialect that SQLAlchemy does "
            "not support."
        ) from None

    if is_sqlite_url(url) and _is_relative_sqlite_file(url):
        url = url.set(database=str(PROJECT_ROOT / url.database))

    return url


def _is_relative_sqlite_file(url: URL) -> bool:
    database = url.database

    if not database or database == ":memory:":
        return False

    # sqlite:///file:...?uri=true is passed to SQLite unchanged.
    if database.startswith("file:") or "uri" in url.query:
        return False

    return not Path(database).is_absolute()


# ---------------------------------------------------------------------------
# Parsing helpers. Messages never contain the supplied value.
# ---------------------------------------------------------------------------


def _read_optional(environ: Mapping[str, str], name: str) -> str | None:
    """The raw value, or None when unset, empty or whitespace only."""

    value = environ.get(name)

    if value is None or not value.strip():
        return None

    return value


def _read_secret_key(environ: Mapping[str, str]) -> str:
    secret_key = _read_optional(environ, "SECRET_KEY")

    if secret_key is None or len(secret_key) < MIN_SECRET_KEY_LENGTH:
        raise ConfigurationError(
            "SECRET_KEY is missing or too short. "
            "Configure it in the project .env file."
        )

    return secret_key


def _read_int(
    environ: Mapping[str, str],
    name: str,
    *,
    default: int,
    maximum: int,
) -> int:
    """A positive integer from 1 to ``maximum``."""

    raw = _read_optional(environ, name)

    if raw is None:
        return default

    text = raw.strip()

    if not _INTEGER.fullmatch(text):
        raise ConfigurationError(f"{name} must be an integer.")

    value = int(text)

    if value <= 0:
        raise ConfigurationError(f"{name} must be positive.")

    if value > maximum:
        raise ConfigurationError(f"{name} must be at most {maximum}.")

    return value


def _read_choice(
    environ: Mapping[str, str],
    name: str,
    choices: tuple[str, ...],
    *,
    default: str,
) -> str:
    raw = _read_optional(environ, name)

    if raw is None:
        return default

    value = raw.strip().lower()

    if value not in choices:
        raise ConfigurationError(
            f"{name} must be one of: {', '.join(choices)}."
        )

    return value


def _read_bool(environ: Mapping[str, str], name: str) -> bool | None:
    raw = _read_optional(environ, name)

    if raw is None:
        return None

    value = raw.strip().lower()

    if value in _TRUE_VALUES:
        return True

    if value in _FALSE_VALUES:
        return False

    raise ConfigurationError(
        f"{name} must be true or false (also accepted: 1/0, yes/no, on/off)."
    )


__all__ = [
    "APP_ENVIRONMENTS",
    "ConfigurationError",
    "DEFAULT_DATABASE_FILE",
    "DEVELOPMENT",
    "ENV_FILE",
    "LOG_FORMATS",
    "PRODUCTION",
    "PROJECT_ROOT",
    "Settings",
    "TEST",
    "default_database_url",
    "is_sqlite_url",
    "load_environment",
    "load_settings",
    "resolve_database_url",
]
