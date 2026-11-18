from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.dependencies import require_agent_or_admin
from app.db.database import get_db
from app.models import User
from app.schemas.analytics import (
    ModelPerformance,
    OperationsAnalytics,
    TicketSummary,
)
from app.services.analytics_service import (
    get_model_performance,
    get_operations_analytics,
    get_ticket_summary,
)

router = APIRouter(tags=["Analytics"])


@router.get(
    "/summary",
    response_model=TicketSummary,
    summary="Retrieve ticket analytics summary",
)
def get_analytics_summary(
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[User, Depends(require_agent_or_admin)],
) -> TicketSummary:
    """Retrieve overall support ticket statistics."""

    summary = get_ticket_summary(db=db)

    return TicketSummary(**summary)


@router.get(
    "/operations",
    response_model=OperationsAnalytics,
    summary="Retrieve operational analytics",
)
def get_analytics_operations(
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[User, Depends(require_agent_or_admin)],
) -> OperationsAnalytics:
    """Retrieve ticket distribution and trend analytics."""

    analytics = get_operations_analytics(db=db)

    return OperationsAnalytics(**analytics)


@router.get("/model-performance", response_model=ModelPerformance)
def read_model_performance(
    _: Annotated[User, Depends(require_agent_or_admin)],
):
    try:
        report = get_model_performance()
        return ModelPerformance(**report)

    except FileNotFoundError:
        raise HTTPException(
            status_code=503,
            detail="Model performance reports are currently unavailable.",
        ) from None

    except (ValueError, KeyError):
        raise HTTPException(
            status_code=500,
            detail="Model performance reports could not be loaded.",
        ) from None