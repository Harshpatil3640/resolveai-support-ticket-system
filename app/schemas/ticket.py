
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


Priority = Literal["Low", "Medium", "High"]

Status = Literal["Open", "In Progress", "Resolved"]

# SQLite's largest INTEGER; larger values cannot be sent to the driver.
MAX_DATABASE_INTEGER = 2**63 - 1

# A JSON integer only ("3", 3.0 and true are rejected).
ExpectedVersion = Annotated[
    int,
    Field(strict=True, ge=1, le=MAX_DATABASE_INTEGER),
]

UserId = Annotated[
    int,
    Field(strict=True, ge=1, le=MAX_DATABASE_INTEGER),
]


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

    # Optional optimistic-concurrency check: the version the client last
    # read. Not a ticket field; it is never written to the ticket.
    expected_version: ExpectedVersion | None = None

    @model_validator(mode="after")
    def validate_updates(self):
        if not self.model_fields_set - {"expected_version"}:
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


class AssigneeSummary(BaseModel):
    """The assigned agent as staff see it: ID and display name only."""

    model_config = ConfigDict(from_attributes=True)

    id: int

    name: str


class CustomerTicketRead(TicketRead):
    """A ticket as a customer sees it.

    Says whether someone is handling the ticket, never who: no assignee
    ID, name or other agent information.
    """

    is_assigned: bool


class StaffTicketRead(TicketRead):
    """A ticket as administrators and agents see it.

    Every field is required, so a customer response can never be
    validated as this model.
    """

    is_assigned: bool

    assigned_to_user_id: int | None

    assignee: AssigneeSummary | None

    assigned_at: datetime | None

    version: int


class TicketEventRead(BaseModel):
    """One ticket history event, as staff see it.

    IDs, fixed event/status/reason values and the timestamp only: no
    names, emails or ticket content of the actor or the assignees.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int

    ticket_id: int

    event_type: str

    actor_user_id: int

    from_assignee_id: int | None

    to_assignee_id: int | None

    from_status: str | None

    to_status: str | None

    reason: str | None

    created_at: datetime


class _TicketActionRequest(BaseModel):
    """Common configuration for the ticket workflow request bodies."""

    model_config = ConfigDict(extra="forbid")


class TicketAssignmentUpdate(_TicketActionRequest):
    """Assign (agent ID) or unassign (explicit null) a ticket.

    Both fields are required; ``assigned_to_user_id`` must be present even
    when it is null.
    """

    assigned_to_user_id: UserId | None

    expected_version: ExpectedVersion


class TicketClaim(_TicketActionRequest):
    """An agent claims an unassigned ticket."""

    expected_version: ExpectedVersion


class TicketStatusUpdate(_TicketActionRequest):
    """A status change, checked against the workflow transition rules."""

    status: Status

    expected_version: ExpectedVersion
