# Shared Package Structure

## Purpose

The `shared` package contains components that are used by more than one service in the system.

Its purpose is to provide a single source of truth for communication contracts and common definitions.

It must remain independent of the Coordinator, Worker, and Dashboard implementations.

---

# Directory Responsibilities

## shared/models

Contains only shared Pydantic models used for communication.

Examples:

- RegisterWorkerRequest
- HeartbeatRequest
- TaskAssignment
- ErrorResponse

Do NOT place:

- Business logic
- Database models
- API endpoints
- Service classes

---

## shared/enums

Contains only enumerations shared by multiple services.

Examples:

- WorkerStatus
- TaskStatus
- ConnectionStatus

Do NOT place:

- Functions
- Utility methods
- Constants

---

## shared/constants

Contains immutable values shared across the project.

Examples:

- Protocol version
- API version
- Timeout defaults
- Endpoint names
- Error codes

Do NOT place:

- Environment variables
- Secrets
- Database URLs
- Runtime configuration

---

## shared/config

Contains configuration schemas only.

These define the structure of configuration objects.

Do NOT place:

- Environment loading
- .env parsing
- Configuration files
- Secrets

---

## shared/protocol

Contains documentation describing how services communicate.

Examples:

- Worker lifecycle
- Task lifecycle
- State machines
- Message flow

Do NOT place:

- Source code
- API implementations
- Networking logic

---

# Design Principles

The shared package should:

- Have no service-specific business logic.
- Be importable by any service.
- Minimize dependencies.
- Serve as the authoritative source for communication contracts.

Any change to a shared model, enum, constant, or protocol should be made here first before updating individual services.