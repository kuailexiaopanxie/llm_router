"""Fixed SQL construction helpers for dashboard read models."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from llm_router.dashboard.models import DashboardFilters


def column(columns: frozenset[str], name: str, default: str = "NULL") -> str:
    """Return a fixed known column expression or its legacy default."""

    return f"r.{name}" if name in columns else default


def filter_sql(
    filters: DashboardFilters,
    columns: frozenset[str],
    attempt_columns: frozenset[str] = frozenset(),
) -> tuple[str, list[Any]]:
    """Build a parameterized predicate from typed dashboard filters."""

    clauses = ["r.received_at >= ?", "r.received_at < ?"]
    values: list[Any] = [filters.start.isoformat(), filters.end.isoformat()]
    mappings: tuple[tuple[tuple[str, ...], str], ...] = (
        (filters.protocols, "protocol"), (filters.endpoint_kinds, "endpoint_kind"),
        (filters.statuses, "status"), (filters.terminal_stages, "terminal_stage"),
        (filters.profiles, "profile"), (filters.models, "final_model"),
        (filters.policy_roles, "policy_role"), (filters.route_reasons, "route_reason"),
    )
    for selected, name in mappings:
        if not selected:
            continue
        if name not in columns:
            clauses.append("0")
            continue
        clauses.append(f"r.{name} IN ({','.join('?' for _ in selected)})")
        values.extend(selected)
    if filters.providers:
        invoked = "fp.upstream_invoked=1" if "upstream_invoked" in attempt_columns else "1"
        clauses.append(
            "EXISTS (SELECT 1 FROM route_attempts fp WHERE fp.request_id=r.request_id "
            f"AND fp.provider IN ({','.join('?' for _ in filters.providers)}) AND {invoked})"
            if attempt_columns else "0"
        )
        values.extend(filters.providers)
    if filters.task_id is not None:
        clauses.append("r.task_id = ?" if "task_id" in columns else "0")
        values.append(str(filters.task_id))
    if filters.fallback is not None:
        expression = fallback_expression(columns, attempt_columns)
        clauses.append(expression if filters.fallback else f"NOT ({expression})")
    return " AND ".join(clauses), values


def fallback_expression(
    columns: frozenset[str], attempt_columns: frozenset[str] = frozenset()
) -> str:
    """Return the fixed actual model-fallback SQL expression."""

    if not {"primary_model", "final_model"}.issubset(columns):
        return "0"
    attempt_clause = (
        " OR EXISTS (SELECT 1 FROM route_attempts fa "
        "WHERE fa.request_id=r.request_id AND fa.sequence>1 AND fa.model<>r.primary_model)"
        if attempt_columns else ""
    )
    return (
        "((r.primary_model NOT IN ('none','unknown') AND r.final_model NOT IN ('none','unknown') "
        f"AND r.primary_model <> r.final_model){attempt_clause})"
    )


def row_value(row: Mapping[str, Any], name: str, default: Any = None) -> Any:
    """Return a decoded row value with a legacy-safe default."""

    try:
        value = row[name]
    except (KeyError, IndexError):
        return default
    return default if value is None else value
