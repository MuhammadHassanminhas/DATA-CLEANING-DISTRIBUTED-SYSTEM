"""
Version 1 API router.

This module aggregates all Version 1 endpoint routers.
"""

from fastapi import APIRouter

from coordinator.api.v1.endpoints.health import router as health_router
from coordinator.api.v1.endpoints.root import router as root_router

api_router = APIRouter()

api_router.include_router(root_router)
api_router.include_router(health_router)