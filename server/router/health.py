"""HTTP routes for system health checks.

- GET    /api/health             → health check
"""

from fastapi import APIRouter

router = APIRouter()


@router.get("/health", tags=["Health"])
def health_check():
    """
    Health check endpoint.
    """
    return {"status": "ok"}
