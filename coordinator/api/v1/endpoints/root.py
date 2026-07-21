"""
Root API endpoints.
"""

from fastapi import APIRouter

from coordinator.api.schemas.root import RootResponse

router = APIRouter()


@router.get(
    "/",
    response_model=RootResponse,
    summary="Root Endpoint",
)
async def root() -> RootResponse:
    """
    Basic root endpoint used to verify that the API is reachable.
    """
    return RootResponse(
        service="Coordinator",
        status="running",
    )