"""Per-task-type timeout policy (Phase 3.1).

Step 3.1's sixth exit criterion is "timeouts configurable per task type
**without redeploy**". That word is what rules out the obvious
implementation: an environment variable needs a process restart, so a
system configured that way would fail the criterion by construction no
matter how many types it supported.

**The shape.** Defaults live in code; overrides live in one Postgres row
per type; a missing row is the normal case and means "use the default".
Overrides are per *column*, so an operator who wants only a longer
execution cap for `sleep` sets that one field and inherits the other two.

**Resolution happens in SQL, not in this process.** The statements that
write a lease resolve their numbers with a correlated subquery against
`task_policies` (see `policy_seconds` below), which is deliberate:

  * a change takes effect on the **next task**, not after a cache TTL
    expires, so "without redeploy" does not quietly become "within ten
    seconds of a redeploy-free change";
  * every replica agrees with no coordination and no invalidation
    message, which is §3.9 — no coordinator instance holds authoritative
    state in memory;
  * the table has at most one row per registered task type — four today —
    so the lookup is a primary-key probe on a table that fits in one page,
    against a hot path that was already writing that row.

The alternative, an in-process cache with a refresh interval, was
rejected on the first of those three: it buys a lookup this cheap at the
cost of making the criterion approximately true.

**Bounds exist because an operator is not infallible.** A lease TTL of 0
would reclaim every task the instant it was assigned — the recovery
system as a denial-of-service against itself — and a 10-year execution
cap would make the deadline meaningless. Neither is malice; both are a
typo. The bounds are recommendations, not measurements (§10).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import (
    task_ack_timeout_seconds,
    task_lease_ttl_seconds,
    task_max_execution_seconds_default,
)
from app.task_types import DEFAULT_MAX_EXECUTION_SECONDS, TASK_TYPES, UnknownTaskType

# The overridable fields. Order is the order they are reported in.
POLICY_FIELDS = (
    "ack_timeout_seconds",
    "lease_ttl_seconds",
    "max_execution_seconds",
    "max_attempts",
)

# Recommendations, not measured values (§10). One second is the smallest
# interval that is not immediately self-defeating; one day is longer than
# any V1 dummy workload can legally run (`sleep` caps at 3600s).
MIN_POLICY_SECONDS = 1
MAX_POLICY_SECONDS = 86_400
# `max_attempts` is Step 3.2's, but the column is validated here so 3.2
# inherits a surface that was never able to store a nonsensical value.
MIN_MAX_ATTEMPTS = 1
MAX_MAX_ATTEMPTS = 100


class InvalidTaskPolicy(ValueError):
    """Raised when an operator submits a policy value outside its bounds."""


def policy_seconds(column: str, default_param: str, *, table_alias: str = "t") -> str:
    """SQL for "this task type's `column`, or the caller's default".

    Returns a bare scalar expression, so the caller decides whether it
    becomes a `make_interval` argument or something else. `table_alias` is
    the alias of the `tasks` row in the surrounding statement, because the
    subquery correlates on that row's own `task_type` — which is the whole
    reason this can resolve a per-type policy inside a statement that
    claims several types at once.

    `column` is never caller data: every call site passes one of
    `POLICY_FIELDS` as a literal, and the guard below is what keeps that
    true if a later edit gets careless with an f-string.
    """
    if column not in POLICY_FIELDS:
        raise ValueError(f"not a policy column: {column!r}")
    return (
        f"COALESCE((SELECT p.{column} FROM task_policies p "
        f"WHERE p.task_type = {table_alias}.task_type), :{default_param})"
    )


def _max_execution_default_case(table_alias: str) -> str:
    """SQL `CASE` mapping each registered type to its code-default cap.

    Built once from `task_types.DEFAULT_MAX_EXECUTION_SECONDS` rather than
    passed as one bound parameter, because the default is **per type** —
    `sleep` legitimately runs for over an hour and `opaque_payload` never
    should — and a statement that claims several types at once cannot carry
    a single number for all of them.

    The keys are the coordinator's own registry, never caller data, and
    the values are formatted as integers, so there is no injection surface
    here. `_MAX_EXECUTION_CASE_GUARD` below is what keeps that true if a
    later edit makes the registry dynamic.
    """
    arms = " ".join(
        f"WHEN '{task_type}' THEN {int(seconds)}"
        for task_type, seconds in sorted(DEFAULT_MAX_EXECUTION_SECONDS.items())
    )
    return f"CASE {table_alias}.task_type {arms} ELSE :max_execution_fallback END"


# Every registry key is a plain identifier. Checked at import rather than
# trusted, because the `CASE` above interpolates them into SQL.
if not all(key.replace("_", "").isalnum() for key in DEFAULT_MAX_EXECUTION_SECONDS):
    raise RuntimeError("a task type name is not a plain identifier; it must not reach SQL")


def max_execution_seconds(*, table_alias: str = "t") -> str:
    """SQL for "this task's execution cap": policy row, else code default."""
    return (
        f"COALESCE((SELECT p.max_execution_seconds FROM task_policies p "
        f"WHERE p.task_type = {table_alias}.task_type), "
        f"{_max_execution_default_case(table_alias)})"
    )


def default_policy(task_type: str) -> dict[str, int | None]:
    """The code defaults for one type — what applies when no row exists.

    `max_attempts` is `None` because Step 3.1 has no attempt cap: the
    reclaimer requeues without limit and Step 3.2 is what adds the policy
    that stops it. Reporting a number here would be inventing one (§10).
    """
    if task_type not in TASK_TYPES:
        raise UnknownTaskType(f"unknown task type: {task_type!r}")
    return {
        "ack_timeout_seconds": task_ack_timeout_seconds(),
        "lease_ttl_seconds": task_lease_ttl_seconds(),
        "max_execution_seconds": DEFAULT_MAX_EXECUTION_SECONDS.get(
            task_type, task_max_execution_seconds_default()
        ),
        "max_attempts": None,
    }


def validate_policy(values: dict[str, Any]) -> dict[str, int]:
    """Check and normalise an operator's submitted overrides.

    Returns only the fields that were actually supplied — a `None` means
    "leave this one alone", not "set it to null", because the two are
    different operator intents and `DELETE` already expresses the second
    one for the whole row.
    """
    cleaned: dict[str, int] = {}
    for field, value in values.items():
        if field not in POLICY_FIELDS:
            raise InvalidTaskPolicy(f"unknown policy field: {field!r}")
        if value is None:
            continue
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise InvalidTaskPolicy(f"{field} must be a whole number") from exc
        low, high = (
            (MIN_MAX_ATTEMPTS, MAX_MAX_ATTEMPTS)
            if field == "max_attempts"
            else (MIN_POLICY_SECONDS, MAX_POLICY_SECONDS)
        )
        if not low <= number <= high:
            raise InvalidTaskPolicy(f"{field} must be between {low} and {high}")
        cleaned[field] = number
    if not cleaned:
        raise InvalidTaskPolicy("no policy fields supplied")
    return cleaned


async def get_overrides(session: AsyncSession) -> dict[str, dict[str, Any]]:
    """Every stored override, keyed by task type. Rows only, no defaults."""
    result = await session.execute(
        text(
            "SELECT task_type, ack_timeout_seconds, lease_ttl_seconds, "
            "max_execution_seconds, max_attempts, updated_at FROM task_policies"
        )
    )
    return {row["task_type"]: dict(row) for row in result.mappings().all()}


async def effective_policies(session: AsyncSession) -> list[dict[str, Any]]:
    """What the coordinator will actually apply, per registered type.

    Reports the resolved number **and** where it came from. That second
    half is the point: an operator reading back a policy needs to know
    whether 300 is their setting or the code's, and a flat list of numbers
    cannot answer it (§10 — a recommended value and a configured one must
    never be blurred).
    """
    overrides = await get_overrides(session)
    report: list[dict[str, Any]] = []
    for task_type in sorted(TASK_TYPES):
        defaults = default_policy(task_type)
        row = overrides.get(task_type, {})
        entry: dict[str, Any] = {"task_type": task_type}
        for field in POLICY_FIELDS:
            override = row.get(field)
            entry[field] = defaults[field] if override is None else override
            entry[f"{field}_source"] = "default" if override is None else "policy"
        entry["updated_at"] = (
            row["updated_at"].isoformat() if row.get("updated_at") is not None else None
        )
        report.append(entry)
    return report


async def set_policy(session: AsyncSession, *, task_type: str, values: dict[str, int]) -> None:
    """Create or update one type's overrides. Does not commit.

    An upsert rather than a delete-and-insert so a concurrent read never
    observes the row missing, and so supplying one field leaves the others
    exactly as they were — `DO UPDATE SET` names only the submitted
    columns.
    """
    if task_type not in TASK_TYPES:
        raise UnknownTaskType(f"unknown task type: {task_type!r}")

    columns = ", ".join(values)
    placeholders = ", ".join(f":{field}" for field in values)
    assignments = ", ".join(f"{field} = EXCLUDED.{field}" for field in values)
    await session.execute(
        text(
            f"INSERT INTO task_policies (task_type, {columns}, updated_at) "
            f"VALUES (:task_type, {placeholders}, now()) "
            f"ON CONFLICT (task_type) DO UPDATE SET {assignments}, updated_at = now()"
        ),
        {"task_type": task_type, **values},
    )


async def clear_policy(session: AsyncSession, *, task_type: str) -> bool:
    """Drop one type's overrides so the code defaults apply again.

    Returns whether a row was removed. Does not commit.
    """
    if task_type not in TASK_TYPES:
        raise UnknownTaskType(f"unknown task type: {task_type!r}")
    result = await session.execute(
        text("DELETE FROM task_policies WHERE task_type = :task_type"),
        {"task_type": task_type},
    )
    return bool(result.rowcount)
