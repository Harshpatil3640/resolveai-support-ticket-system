
from datetime import datetime, timezone

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RevokedToken(Base):
    __tablename__ = "revoked_tokens"

    # Unique identifier of the JWT.
    jti: Mapped[str] = mapped_column(
        String(128),
        primary_key=True,
    )

    # Original expiry time of the JWT.
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )

    # Time when the token was revoked.
    revoked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
