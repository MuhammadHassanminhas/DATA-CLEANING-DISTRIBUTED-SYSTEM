from pydantic import BaseModel


class HeartbeatRequest(BaseModel):
    worker_id: str


class HeartbeatResponse(BaseModel):
    accepted: bool