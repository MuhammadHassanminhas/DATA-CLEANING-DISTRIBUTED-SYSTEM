from fastapi import FastAPI

from coordinator.app.config.settings import get_settings
from coordinator.app.core.logging import configure_logging
from coordinator.app.lifecycle.events import register_lifecycle_events


def create_app() -> FastAPI:
    """
    Create and configure the Coordinator FastAPI application.
    """

    # Load application configuration
    settings = get_settings()

    # Create the FastAPI application
    app = FastAPI(
        title=settings.app_name,
        description="Coordinator for the Distributed AI-Orchestrated SQL Database Cleaning Platform",
        version=settings.app_version,
    )

    # Configure application infrastructure
    configure_logging()
    register_lifecycle_events(app)

    return app