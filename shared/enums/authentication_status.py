from enum import Enum


class AuthenticationStatus(str, Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"