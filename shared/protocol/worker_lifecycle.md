# Worker Lifecycle

A worker progresses through the following states:

NEW
↓
REGISTERED
↓
AUTHENTICATED
↓
IDLE
↓
BUSY
↓
IDLE
↓
DISCONNECTED
↓
OFFLINE

Description:

- NEW: Worker process starts.
- REGISTERED: Worker receives a unique identifier.
- AUTHENTICATED: Worker successfully authenticates.
- IDLE: Worker is waiting for work.
- BUSY: Worker is executing a task.
- DISCONNECTED: Connection to the coordinator is temporarily lost.
- OFFLINE: Worker is no longer considered active.