from pydantic import BaseModel


class CoordinatorConfig(BaseModel):
    host: str
    port: int