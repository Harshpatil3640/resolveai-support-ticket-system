"""Validated application configuration, loaded once at import time.

Settings are parsed by ``app.core.settings`` (see its documentation for
every variable). Importing this module loads the project ``.env`` file and
raises ``ConfigurationError`` (a ``RuntimeError``) if any setting is
missing or invalid, so the application fails at start-up instead of
running with an unsafe configuration.

The module-level names below are kept for existing imports.
"""

import os

from app.core.settings import (
    PROJECT_ROOT,
    ConfigurationError,
    Settings,
    load_environment,
    load_settings,
)


# Load environment variables from the project root (never overriding
# variables already set in the real environment).
load_environment()

# Every validated setting.
settings: Settings = load_settings(os.environ)


# JWT configuration.
SECRET_KEY = settings.secret_key

ALGORITHM = "HS256"

ACCESS_TOKEN_EXPIRE_MINUTES = settings.access_token_expire_minutes


__all__ = [
    "ACCESS_TOKEN_EXPIRE_MINUTES",
    "ALGORITHM",
    "PROJECT_ROOT",
    "SECRET_KEY",
    "ConfigurationError",
    "settings",
]
