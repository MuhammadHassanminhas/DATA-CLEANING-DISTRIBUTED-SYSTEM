from pydantic import BaseModel


class Settings(BaseModel):
    app_name: str = "Coordinator Service"
    app_version: str = "0.1.0"


def get_settings() -> Settings:
    return Settings()