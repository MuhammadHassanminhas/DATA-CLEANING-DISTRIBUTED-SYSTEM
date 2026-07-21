from pydantic import BaseModel


class DashboardConfig(BaseModel):
    host: str
    port: int