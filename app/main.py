from fastapi import FastAPI

app = FastAPI(
    title = "ResolveAI API",
    description = "API for the ResolveAI customer support ticket management system.",
    version = "0.1.0",
)


@app.get("/api/health",tags=["Health"])
def health_check() -> dict[str,str]:
    """Return the health status of the ResolveAI API."""
    return {
        "status": "healthy",
        "application": "ResolveAI",
    }