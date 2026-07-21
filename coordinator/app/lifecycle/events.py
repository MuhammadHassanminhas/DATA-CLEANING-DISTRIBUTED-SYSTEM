from contextlib import asynccontextmanager

from fastapi import FastAPI


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Coordinator application lifecycle.

    Startup and shutdown logic will be added incrementally
    in later phases.
    """

    # Startup
    yield

    # Shutdown


def register_lifecycle_events(app: FastAPI) -> None:
    """
    Register the application's lifecycle.
    """

    app.router.lifespan_context = lifespan