import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError


from app.api.routes.auth import router as auth_router
from app.api.routes.tickets import router as tickets_router
from app.api.routes.analytics import router as analytics_router
from app.db.database import engine
from app.db.errors import (
    describe_database_error,
    describe_rollback_failure,
    rollback_failure_of,
    separate_rollback_failure,
)
from app.db.safe_logging import sanitised_database_logs

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

logger = logging.getLogger(__name__)

DATABASE_ERROR_DETAIL = "Internal server error."


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Application-lifetime setup.

    For as long as the API runs, SQLAlchemy's connection-pool log records
    are sanitised (``app.db.safe_logging``), so a failing rollback or
    connection reset can never print the database driver's message or a
    traceback. The filter is removed on shutdown, and re-attached if the
    application starts again.
    """

    with sanitised_database_logs(engine):
        yield


app = FastAPI(
    title="ResolveAI API",
    description="API for the ResolveAI customer support ticket management system.",
    version="0.1.0",
    lifespan=lifespan,
)


@app.exception_handler(SQLAlchemyError)
async def database_error_handler(
    _request: Request,
    error: SQLAlchemyError,
) -> JSONResponse:
    """Answer an unhandled database error without leaking its details.

    Without this handler the exception would reach the server, which logs
    it with a full traceback containing the SQL and the driver's message.
    Instead a fixed message with the allow-listed diagnostic is logged (no
    traceback), the original failure kept primary if a rollback also
    failed, and the client receives a generic 500 response.
    """

    primary, rollback_failure = separate_rollback_failure(error)
    rollback_failure = rollback_failure_of(primary) or rollback_failure

    if rollback_failure is None:
        logger.error(
            "Database error while handling a request (%s).",
            describe_database_error(primary),
        )
    else:
        logger.error(
            "Database error while handling a request (%s). "
            "The rollback also failed (%s).",
            describe_database_error(primary),
            describe_rollback_failure(rollback_failure),
        )

    return JSONResponse(
        status_code=500,
        content={"detail": DATABASE_ERROR_DETAIL},
    )


@app.get("/api/health", tags=["Health"])
def health_check() -> dict[str, str]:
    """Return the health status of the ResolveAI API."""

    return {
        "status": "healthy",
        "application": "ResolveAI",
    }


# Register authentication endpoints.
app.include_router(
    auth_router,
    prefix="/api/auth",
)


# Register ticket management endpoints.
app.include_router(
    tickets_router,
    prefix="/api/tickets",
)

# Register analytics endpoints.
app.include_router(
    analytics_router,
    prefix="/api/analytics",
)


# Serve the frontend application.
app.mount(
    "/",
    StaticFiles(directory=FRONTEND_DIR, html=True),
    name="frontend",
)
