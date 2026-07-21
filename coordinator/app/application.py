from fastapi import FastAPI

from coordinator.api.v1.router import api_router
from coordinator.app.config.settings import get_settings
from coordinator.app.core.logging import configure_logging
from coordinator.app.lifecycle.events import register_lifecycle_events


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        description=(
            "Coordinator API for the Distributed AI-Orchestrated "
            "SQL Database Cleaning Platform."
        ),
        version=settings.app_version,
        openapi_tags=[
            {
                "name": "Version 1",
                "description": "Version 1 API endpoints.",
            },
            {
                "name": "Health",
                "description": "Service health and liveness endpoints.",
            },
        ],
    )

    app.include_router(
        api_router,
        prefix="/api/v1",
        tags=["Version 1"],
    )

    # Make configuration available application-wide
    app.state.settings = settings

    configure_logging()
    register_lifecycle_events(app)

    return app