from pydantic import BaseModel


class LoggingConfig(BaseModel):
    level: str
    format: str