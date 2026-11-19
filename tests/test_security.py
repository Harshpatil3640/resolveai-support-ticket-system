
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
    token = create_access_token(subject="123", token_version=0)

    payload = decode_access_token(token)

    assert payload["sub"] == "123"
    assert payload["token_type"] == "access"
    assert payload["jti"]


def test_expired_token_is_rejected() -> None:
    token = create_access_token(
        subject="123",
        expires_delta=timedelta(seconds=-1),
        token_version=0,
    )

    with pytest.raises(jwt.ExpiredSignatureError):
        decode_access_token(token)


def test_tampered_token_is_rejected() -> None:
    token = create_access_token(subject="123", token_version=0)

    tampered_token = token[:-1] + (
        "A" if token[-1] != "A" else "B"
    )

    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token(tampered_token)


def test_empty_token_subject_is_rejected() -> None:
    with pytest.raises(ValueError):
        create_access_token(subject="", token_version=0)


# ---------------------------------------------------------------------------
# Token version claim
# ---------------------------------------------------------------------------


def test_token_contains_token_version() -> None:
    token = create_access_token(subject="123", token_version=7)

    payload = decode_access_token(token)

    assert payload["token_version"] == 7


@pytest.mark.parametrize("bad_version", [-1, True, "1", None, 1.0])
def test_create_access_token_rejects_invalid_version(
    bad_version: object,
) -> None:
    with pytest.raises(ValueError):
        create_access_token(
            subject="123",
            token_version=bad_version,  # type: ignore[arg-type]
        )


def test_create_access_token_requires_token_version() -> None:
    with pytest.raises(TypeError):
        create_access_token(subject="123")  # type: ignore[call-arg]


def test_decode_rejects_token_without_version(forge_access_token) -> None:
    legacy_token = forge_access_token(subject="123", omit=("token_version",))

    with pytest.raises(jwt.MissingRequiredClaimError):
        decode_access_token(legacy_token)


@pytest.mark.parametrize("bad_version", ["0", True, -1, 1.5, None])
def test_decode_rejects_invalid_version_values(
    forge_access_token,
    bad_version: object,
) -> None:
    token = forge_access_token(subject="123", token_version=bad_version)

    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token(token)
