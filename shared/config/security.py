from pydantic import BaseModel


class SecurityConfig(BaseModel):
    authentication_enabled: bool
    tls_enabled: bool