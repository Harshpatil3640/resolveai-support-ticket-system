
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user, oauth2_scheme
from app.core.security import create_access_token
from app.db.database import get_db
from app.models import User
from app.schemas import MessageResponse, TokenResponse, UserRead
from app.services.auth_service import (
    authenticate_user,
    revoke_access_token,
)


router = APIRouter(tags=["Authentication"])


@router.post("/login", response_model=TokenResponse)
def login(
    form_data: Annotated[
        OAuth2PasswordRequestForm,
        Depends(),
    ],
    db: Annotated[Session, Depends(get_db)],
) -> TokenResponse:
    """Authenticate a support agent and issue an access token."""

    user = authenticate_user(
        db=db,
        email=form_data.username,
        password=form_data.password,
    )

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token = create_access_token(
        subject=str(user.id)
    )

    return TokenResponse(access_token=access_token)


@router.get("/me", response_model=UserRead)
def get_my_profile(
    current_user: Annotated[
        User,
        Depends(get_current_user),
    ],
) -> User:
    """Return the currently authenticated user's profile."""

    return current_user


@router.post("/logout", response_model=MessageResponse)
def logout(
    token: Annotated[str, Depends(oauth2_scheme)],
    current_user: Annotated[
        User,
        Depends(get_current_user),
    ],
    db: Annotated[Session, Depends(get_db)],
) -> MessageResponse:
    """Revoke the current user's access token."""

    revoked = revoke_access_token(
        db=db,
        token=token,
    )

    if not revoked:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired access token.",
        )

    return MessageResponse(
        message="Successfully logged out."
    )
