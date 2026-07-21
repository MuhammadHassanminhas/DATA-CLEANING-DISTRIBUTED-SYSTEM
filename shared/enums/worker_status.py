from enum import Enum


class WorkerStatus(str, Enum):
    NEW = "NEW"
    REGISTERED = "REGISTERED"
    AUTHENTICATED = "AUTHENTICATED"
    IDLE = "IDLE"
    BUSY = "BUSY"
    OFFLINE = "OFFLINE"
    DISCONNECTED = "DISCONNECTED"