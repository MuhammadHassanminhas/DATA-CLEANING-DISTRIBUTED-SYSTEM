from pydantic import BaseModel


class RegisterWorkerRequest(BaseModel):
    worker_name: str
    worker_version: str
    platform: str


class RegisterWorkerResponse(BaseModel):
    worker_id: str
    success: bool
    message: str