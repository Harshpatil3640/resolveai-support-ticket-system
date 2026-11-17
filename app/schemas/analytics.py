from datetime import date
from typing import Literal
from pydantic import BaseModel, Field


class TicketSummary(BaseModel):
    """Represent the overall support ticket statistics."""

    total_tickets: int = Field(ge=0)

    open_tickets: int = Field(ge=0)

    in_progress_tickets: int = Field(ge=0)

    resolved_tickets: int = Field(ge=0)



class CategoryCount(BaseModel):
    """Represent ticket counts for a category."""

    category: str
    count: int = Field(ge=0)


class PriorityCount(BaseModel):
    """Represent ticket counts for a priority level."""

    priority: Literal["Low", "Medium", "High"]
    count: int = Field(ge=0)


class DailyTicketCount(BaseModel):
    """Represent ticket creation counts by date."""

    date: date
    count: int = Field(ge=0)


class CategoryMetrics(BaseModel):
    category: str
    precision: float = Field(ge=0, le=1)
    recall: float = Field(ge=0, le=1)
    f1_score: float = Field(ge=0, le=1)
    support: int = Field(ge=0)


class ConfusionMatrix(BaseModel):
    labels: list[str]
    values: list[list[int]]


class ModelPerformance(BaseModel):
    model: str
    training_records: int = Field(ge=0)
    test_records: int = Field(ge=0)
    accuracy: float = Field(ge=0, le=1)
    macro_f1: float = Field(ge=0, le=1)
    weighted_f1: float = Field(ge=0, le=1)
    category_metrics: list[CategoryMetrics]
    confusion_matrix: ConfusionMatrix


class OperationsAnalytics(BaseModel):
    """Represent operational analytics for the support system."""

    summary: TicketSummary

    category_distribution: list[CategoryCount]

    ai_prediction_distribution: list[CategoryCount]

    priority_distribution: list[PriorityCount]

    ticket_trends: list[DailyTicketCount]