from pydantic import BaseModel
from typing import Any


class TaskRequest(BaseModel):
    worker_id: str


class TaskAssignment(BaseModel):
    task_id: str
    task_type: str
    payload: Any


class TaskResult(BaseModel):
    task_id: str
    success: bool
    result: Any