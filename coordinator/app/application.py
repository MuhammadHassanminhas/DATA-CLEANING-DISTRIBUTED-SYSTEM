from fastapi import FastAPI


def create_app() -> FastAPI:
    """
    Create and configure the Coordinator FastAPI application.

    Future infrastructure (configuration, logging, middleware,
    routers, lifecycle, etc.) will be added incrementally.
    """

    app = FastAPI(
        title="Coordinator Service",
        description="Coordinator for the Distributed AI-Orchestrated SQL Database Cleaning Platform",
        version="0.1.0",
    )

    return app