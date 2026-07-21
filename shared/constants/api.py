"""
Shared API constants.
"""

API_VERSION = "v1"

API_PREFIX = f"/api/{API_VERSION}"

HEALTH_ENDPOINT = "/health"

REGISTER_ENDPOINT = "/workers/register"

AUTHENTICATE_ENDPOINT = "/workers/authenticate"

HEARTBEAT_ENDPOINT = "/workers/heartbeat"

TASK_REQUEST_ENDPOINT = "/tasks/request"

TASK_RESULT_ENDPOINT = "/tasks/result"