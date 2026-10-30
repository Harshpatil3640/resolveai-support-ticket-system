
from datetime import timedelta

import jwt
import pytest

from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)


def test_password_hashing() -> None:
    password = "TestPassword123!"

    hashed = hash_password(password)

    assert hashed != password
    assert verify_password(password, hashed) is True


def test_incorrect_password() -> None:
    hashed = hash_password("CorrectPassword123!")

    assert verify_password(
        "WrongPassword123!",
        hashed,
    ) is False


def test_password_hashes_are_unique() -> None:
    password = "TestPassword123!"

    first_hash = hash_password(password)
    second_hash = hash_password(password)

    assert first_hash != second_hash


def test_create_and_decode_access_token() -> None:
    token = create_access_token(subject="123")

    payload = decode_access_token(token)

    assert payload["sub"] == "123"
    assert payload["token_type"] == "access"
    assert payload["jti"]


def test_expired_token_is_rejected() -> None:
    token = create_access_token(
        subject="123",
        expires_delta=timedelta(seconds=-1),
    )

    with pytest.raises(jwt.ExpiredSignatureError):
        decode_access_token(token)


def test_tampered_token_is_rejected() -> None:
    token = create_access_token(subject="123")

    tampered_token = token[:-1] + (
        "A" if token[-1] != "A" else "B"
    )

    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token(tampered_token)


def test_empty_token_subject_is_rejected() -> None:
    with pytest.raises(ValueError):
        create_access_token(subject="")
