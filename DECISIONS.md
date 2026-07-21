# DECISIONS.md

# Architecture Decision Log

---

## ADR-001

Status: Active

Title:
Contract-First Development

Context:

The project is a distributed system where multiple services must communicate reliably.

Decision:

Define communication contracts before implementing services.

Reasoning:

Prevents incompatible implementations between Coordinator and Worker.

Alternatives:

Implement Coordinator first.

Rejected because contracts would evolve unpredictably.

Consequences:

Shared models become the authoritative communication layer.

---

## ADR-002

Status: Active

Title:
Dedicated Shared Package

Context:

Coordinator, Worker and Dashboard require common definitions.

Decision:

Create a dedicated shared package.

Reasoning:

Avoid duplicate implementations.

Maintain one source of truth.

Alternatives:

Duplicate models inside every service.

Rejected.

---

## ADR-003

Status: Active

Title:
Pydantic for Shared Models

Context:

Communication contracts require validation.

Decision:

Use Pydantic BaseModel.

Reasoning:

Strong typing.

Validation.

FastAPI compatibility.

Serialization support.

Alternatives:

Dataclasses

TypedDict

Plain dictionaries

Rejected.

---

## ADR-004

Status: Active

Title:
Centralized Enumerations

Decision:

All shared state values must be defined once inside shared/enums.

Reasoning:

Prevent inconsistent state definitions.

---

## ADR-005

Status: Active

Title:
Centralized Constants

Decision:

Protocol constants, API constants, timeout values and error codes must be centralized.

Reasoning:

Avoid duplicated literals.

---

## ADR-006

Status: Active

Title:
Configuration Schemas Separate from Configuration Loading

Decision:

Shared package contains only configuration schemas.

Loading configuration belongs to individual services.

Reasoning:

Separates contracts from implementation.

---

## ADR-007

Status: Active

Title:
Documentation-Driven Protocol

Decision:

Worker lifecycle, task lifecycle, message flow and versioning are documented before implementation.

Reasoning:

Documentation becomes the protocol specification.

---

## ADR-008

Status: Active

Title:
Strict Incremental Development

Decision:

Only one implementation step may be completed at a time.

Every step must be validated before continuing.

Reasoning:

Reduces accumulated technical debt.

Simplifies debugging.

Ensures demonstrable milestones.