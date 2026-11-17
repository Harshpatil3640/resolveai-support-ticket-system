from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles


from app.api.routes.auth import router as auth_router
from app.api.routes.tickets import router as tickets_router
from app.api.routes.analytics import router as analytics_router

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
app = FastAPI(
    title="ResolveAI API",
    description="API for the ResolveAI customer support ticket management system.",
    version="0.1.0",
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
