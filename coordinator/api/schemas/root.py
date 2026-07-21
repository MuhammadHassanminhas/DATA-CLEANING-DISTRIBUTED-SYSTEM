from pydantic import BaseModel


class RootResponse(BaseModel):
    """
    Response returned by the root endpoint.
    """

    service: str
    status: str