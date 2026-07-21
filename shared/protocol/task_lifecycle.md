# Task Lifecycle

Each task follows the lifecycle below:

CREATED
↓
QUEUED
↓
ASSIGNED
↓
RUNNING
↓
COMPLETED

Failure paths:

RUNNING
↓
FAILED

RUNNING
↓
TIMEOUT

ASSIGNED
↓
CANCELLED