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
from app.api.routes.admin import router as admin_router
from app.api.routes.notifications import router as notifications_router
from app.core.audit_logging import configure_audit_logging
from app.core.config import settings
from app.core.settings import Settings
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

# Administrative actions are logged at INFO, which an unconfigured logger
# (or Uvicorn's default logging configuration) would drop. Configured here
# so the audit log works even if the server does not run the lifespan.
configure_audit_logging()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Application-lifetime setup.

    For as long as the API runs, SQLAlchemy's connection-pool log records
    are sanitised (``app.db.safe_logging``), so a failing rollback or
    connection reset can never print the database driver's message or a
    traceback. The filter is removed on shutdown, and re-attached if the
    application starts again.

    The audit logger is configured again on every start-up (idempotent),
    in case a logging configuration applied after import disabled it.
    """

    configure_audit_logging()

    with sanitised_database_logs(engine):
        yield


API_DOCS_URL = "/docs"
API_REDOC_URL = "/redoc"
API_OPENAPI_URL = "/openapi.json"


def api_documentation_urls(app_settings: Settings) -> dict[str, str | None]:
    """FastAPI's documentation URLs for these settings.

    Development and test serve /docs, /redoc and /openapi.json (FastAPI's
    defaults); production disables all three unless ENABLE_API_DOCS=true.
    ENABLE_API_DOCS=false disables them in any environment. A disabled URL
    is not registered, so the path falls through to the static frontend,
    which answers 404.
    """

    if app_settings.api_docs_enabled:
        return {
            "docs_url": API_DOCS_URL,
            "redoc_url": API_REDOC_URL,
            "openapi_url": API_OPENAPI_URL,
        }

    return {"docs_url": None, "redoc_url": None, "openapi_url": None}


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


def health_check() -> dict[str, str]:
    """Return the health status of the ResolveAI API."""

    return {
        "status": "healthy",
        "application": "ResolveAI",
    }


def create_app(app_settings: Settings | None = None) -> FastAPI:
    """Build the ResolveAI application.

    ``app_settings`` defaults to the validated settings loaded at import
    (``app.core.config.settings``); tests pass their own to check, for
    example, the production behaviour. Only the documentation URLs depend
    on the settings: every API route is the same in all environments. The
    database is never migrated here (see docs/database-migrations.md).
    """

    app_settings = app_settings or settings

    application = FastAPI(
        title="ResolveAI API",
        description=(
            "API for the ResolveAI customer support ticket management system."
        ),
        version="0.1.0",
        lifespan=lifespan,
        **api_documentation_urls(app_settings),
    )

    application.add_exception_handler(SQLAlchemyError, database_error_handler)

    application.add_api_route(
        "/api/health",
        health_check,
        methods=["GET"],
        tags=["Health"],
    )

    # Register authentication endpoints.
    application.include_router(
        auth_router,
        prefix="/api/auth",
    )

    # Register ticket management endpoints.
    application.include_router(
        tickets_router,
        prefix="/api/tickets",
    )

    # Register analytics endpoints.
    application.include_router(
        analytics_router,
        prefix="/api/analytics",
    )

    # Register admin-only user management endpoints.
    application.include_router(
        admin_router,
        prefix="/api/admin",
    )

    # Register the authenticated user's notification endpoints.
    application.include_router(
        notifications_router,
        prefix="/api/notifications",
    )

    # Serve the frontend application.
    application.mount(
        "/",
        StaticFiles(directory=FRONTEND_DIR, html=True),
        name="frontend",
    )

    return application


app = create_app()
