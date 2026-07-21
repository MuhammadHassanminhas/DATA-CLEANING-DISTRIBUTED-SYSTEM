from pydantic import BaseModel


class WorkerConfig(BaseModel):
    coordinator_url: str
    reconnect_delay: int