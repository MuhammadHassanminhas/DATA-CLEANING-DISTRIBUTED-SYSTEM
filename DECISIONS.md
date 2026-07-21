# DECISIONS.md

# Architectural Decision Log

---

## Decision ID
ADR-001

### Title
Use an Application Factory

### Context

The Coordinator service requires a single location responsible for application construction.

### Decision

Construct the FastAPI application using a dedicated `create_app()` factory.

### Reasoning

- Separation of concerns
- Easier testing
- Centralized initialization

### Alternatives Considered

- Direct application construction inside `main.py`

### Tradeoffs

Slightly more structure for a small project but significantly better scalability.

### Consequences

All future infrastructure initialization occurs through the application factory.

### Status

**Active**

---

## Decision ID
ADR-002

### Title
Modular Infrastructure Layout

### Context

Infrastructure responsibilities should remain isolated.

### Decision

Separate infrastructure into dedicated modules.

### Reasoning

Improves maintainability and scalability.

### Alternatives Considered

Single monolithic application module.

### Tradeoffs

More files in exchange for cleaner architecture.

### Consequences

Infrastructure evolves independently.

### Status

**Active**

---

## Decision ID
ADR-003

### Title
Use FastAPI Lifespan Events

### Context

Modern FastAPI recommends lifespan over legacy startup/shutdown events.

### Decision

Adopt the lifespan API.

### Reasoning

Future-proof and officially recommended.

### Alternatives Considered

`@app.on_event`

### Tradeoffs

Requires slightly different initialization style.

### Consequences

Lifecycle management remains modern and maintainable.

### Status

**Active**

---

## Decision ID
ADR-004

### Title
Versioned API Architecture

### Context

The Coordinator API will evolve over time.

### Decision

Organize endpoints under version-specific packages (`api/v1`).

### Reasoning

Supports future API versions without breaking existing clients.

### Alternatives Considered

Single unversioned API.

### Tradeoffs

Additional package structure.

### Consequences

Future versions (`v2`, `v3`) can coexist cleanly.

### Status

**Active**

---

## Decision ID
ADR-005

### Title
Central API Router Aggregation

### Context

Endpoint routers require a single integration point.

### Decision

Use a dedicated `api_router` to aggregate all Version 1 endpoint routers.

### Reasoning

Provides modular endpoint registration.

### Alternatives Considered

Register every endpoint directly in the application factory.

### Tradeoffs

One additional routing layer.

### Consequences

Future endpoint modules integrate consistently.

### Status

**Active**

---

## Decision ID
ADR-006

### Title
Separate API Schemas from Endpoints

### Context

Response contracts should remain independent from routing logic.

### Decision

Introduce dedicated Pydantic response models.

### Reasoning

Improves documentation, validation, maintainability, and type safety.

### Alternatives Considered

Return anonymous dictionaries.

### Tradeoffs

Requires additional schema modules.

### Consequences

Stable API contracts and improved OpenAPI generation.

### Status

**Active**

---

## Decision ID
ADR-007

### Title
Dedicated Health Endpoint

### Context

Operational health checks should remain separate from service identification.

### Decision

Expose a dedicated `/health` endpoint.

### Reasoning

Supports future readiness and liveness checks.

### Alternatives Considered

Reuse the root endpoint.

### Tradeoffs

Additional endpoint.

### Consequences

Health monitoring can evolve independently.

### Status

**Active**