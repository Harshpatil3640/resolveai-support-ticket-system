
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


Priority = Literal["Low", "Medium", "High"]

Status = Literal["Open", "In Progress", "Resolved"]


class TicketCreate(BaseModel):
    """Validate data submitted when creating a ticket."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    title: str = Field(min_length=3, max_length=200)

    description: str = Field(min_length=5)

    requester_name: str | None = Field(
        default=None,
        max_length=100,
    )

    priority: Priority = "Medium"


class TicketUpdate(BaseModel):
    """Validate fields that can be updated by an agent."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    title: str | None = Field(
        default=None,
        min_length=3,
        max_length=200,
    )

    description: str | None = Field(
        default=None,
        min_length=5,
    )

    requester_name: str | None = Field(
        default=None,
        max_length=100,
    )

    category: str | None = Field(
        default=None,
        max_length=100,
    )

    priority: Priority | None = None

    status: Status | None = None

    @model_validator(mode="after")
    def validate_updates(self):
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided.")

        non_nullable_fields = {
            "title",
            "description",
            "priority",
            "status",
        }

        for field in non_nullable_fields:
            if field in self.model_fields_set:
                if getattr(self, field) is None:
                    raise ValueError(
                        f"{field} cannot be null."
                    )

        return self


class TicketRead(BaseModel):
    """Define the ticket data returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: int

    title: str

    description: str

    requester_name: str | None

    predicted_category: str | None

    category: str | None

    prediction_confidence: float | None

    priority: Priority

    status: Status

    created_by_user_id: int

    created_at: datetime

    updated_at: datetime
