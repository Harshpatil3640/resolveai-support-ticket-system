
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from pwdlib import PasswordHash

from app.core.config import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    ALGORITHM,
    SECRET_KEY,
)


# Configure the recommended password hashing algorithm.
password_hasher = PasswordHash.recommended()


def hash_password(password: str) -> str:
    """Generate a secure one-way password hash."""

    return password_hasher.hash(password)


def verify_password(
    plain_password: str,
    hashed_password: str,
) -> bool:
    """Verify a supplied password against its stored hash."""

    return password_hasher.verify(
        plain_password,
        hashed_password,
    )


def create_access_token(
    subject: str,
    expires_delta: timedelta | None = None,
) -> str:
    """Generate a signed JWT access token."""

    if not subject or not subject.strip():
        raise ValueError("Token subject cannot be empty.")

    now = datetime.now(timezone.utc)

    expiry = now + (
        expires_delta
        or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )

    payload = {
        "sub": subject,
        "iat": now,
        "exp": expiry,
        "jti": secrets.token_urlsafe(16),
        "token_type": "access",
    }

    token = jwt.encode(
        payload,
        SECRET_KEY,
        algorithm=ALGORITHM,
    )

    return token


def decode_access_token(token: str) -> dict:
    """Verify a JWT signature, expiration and required claims."""

    payload = jwt.decode(
        token,
        SECRET_KEY,
        algorithms=[ALGORITHM],
        options={
            "require": ["sub", "iat", "exp", "jti"],
        },
    )

    if payload.get("token_type") != "access":
        raise jwt.InvalidTokenError("Invalid token type.")

    return payload
