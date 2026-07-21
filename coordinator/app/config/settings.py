from pydantic import BaseModel


class Settings(BaseModel):
    """
    Coordinator application configuration.

    This class defines the configuration contract for the
    Coordinator service.

    Configuration loading will be implemented in a later step.
    """

    app_name: str = "Coordinator Service"
    app_version: str = "0.1.0"


def get_settings() -> Settings:
    """
    Return the Coordinator application's settings.

    Future implementations will load configuration from
    environment variables and configuration files.

    For now, return the default configuration.
    """
    return Settings()