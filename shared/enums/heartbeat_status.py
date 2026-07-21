from enum import Enum


class HeartbeatStatus(str, Enum):
    OK = "OK"
    LATE = "LATE"
    MISSED = "MISSED"