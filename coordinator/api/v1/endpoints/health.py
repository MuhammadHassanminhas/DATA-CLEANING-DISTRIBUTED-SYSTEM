"""
Health check endpoints.
"""

from fastapi import APIRouter

from coordinator.api.schemas.health import HealthResponse

router = APIRouter(
    prefix="/health",
    tags=["Health"],
)


@router.get(
    "/",
    response_model=HealthResponse,
    summary="Health Check",
)
async def health_check() -> HealthResponse:
    """
    Basic liveness probe for the Coordinator service.
    """
    return HealthResponse(
        status="healthy",
    )