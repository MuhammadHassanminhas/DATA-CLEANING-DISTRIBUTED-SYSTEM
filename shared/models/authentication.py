from pydantic import BaseModel


class AuthenticateRequest(BaseModel):
    worker_id: str
    token: str


class AuthenticateResponse(BaseModel):
    success: bool
    access_token: str
    expires_in: int