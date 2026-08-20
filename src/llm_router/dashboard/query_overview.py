"""SQLite-side Overview aggregation and bounded breakdowns."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from llm_router.dashboard.models import DashboardFilters
from llm_router.dashboard.query_sql import column, fallback_expression, filter_sql

_BUCKETS = ((6 * 3600, "5m", 300), (48 * 3600, "1h", 3600),
            (14 * 86400, "6h", 21600), (90 * 86400, "1d", 86400))


def _ratio(numerator: int, denominator: int) -> float | None:
    """Calculate one ratio while preserving an empty denominator."""

    return numerator / denominator if denominator else None


def _bucket(filters: DashboardFilters) -> tuple[str, int]:
    """Resolve automatic time bucket width from the fixed contract."""

    if filters.bucket != "auto":
        return filters.bucket, {"5m": 300, "1h": 3600, "6h": 21600, "1d": 86400, "7d": 604800}[filters.bucket]
    seconds = (filters.end - filters.start).total_seconds()
    for maximum, name, width in _BUCKETS:
        if seconds <= maximum:
            return name, width
    return "7d", 604800


def _percentile(connection: sqlite3.Connection, where: str, values: list[Any], percentile: float) -> float | None:
    """Compute nearest-rank latency using a bounded SQLite window query."""

    row = connection.execute(
        f"""WITH ranked AS (
          SELECT total_latency_ms AS value, ROW_NUMBER() OVER (ORDER BY total_latency_ms) AS rn,
                 COUNT(*) OVER () AS n FROM route_requests r
          WHERE {where} AND total_latency_ms >= 0 AND total_latency_ms < 1e308)
        SELECT value FROM ranked WHERE rn=CAST((? * n + 0.999999999) AS INTEGER) LIMIT 1""",
        (*values, percentile),
    ).fetchone()
    return float(row[0]) if row else None


def _summary(connection: sqlite3.Connection, where: str, values: list[Any], columns: frozenset[str], attempt_columns: frozenset[str]) -> dict[str, Any]:
    """Aggregate request, fallback, latency, and cost coverage facts."""

    fallback = fallback_expression(columns, attempt_columns)
    routed = "r.primary_model NOT IN ('none','unknown')" if "primary_model" in columns else "0"
    cost_status = column(columns, "cost_status", "'usage_missing'")
    row = connection.execute(
        f"""SELECT COUNT(*) requests,
          SUM(CASE WHEN r.status='success' THEN 1 ELSE 0 END) successes,
          SUM(CASE WHEN {fallback} THEN 1 ELSE 0 END) fallbacks,
          SUM(CASE WHEN {routed} THEN 1 ELSE 0 END) fallback_known,
          SUM(CASE WHEN NOT ({routed}) THEN 1 ELSE 0 END) fallback_unknown,
          SUM(CASE WHEN {cost_status}='complete' THEN 1 ELSE 0 END) cost_complete,
          SUM(CASE WHEN {cost_status}='partial' THEN 1 ELSE 0 END) cost_partial,
          SUM(CASE WHEN {cost_status}='unpriced' THEN 1 ELSE 0 END) cost_unpriced,
          SUM(CASE WHEN {cost_status}='not_applicable' THEN 1 ELSE 0 END) cost_na,
          SUM(CASE WHEN {cost_status} IS NULL OR {cost_status}='usage_missing' THEN 1 ELSE 0 END) cost_missing,
          SUM(COALESCE({column(columns, 'unknown_cost_attempts', '0')},0)) unknown_attempts
        FROM route_requests r WHERE {where}""", values
    ).fetchone()
    requests = int(row["requests"] or 0)
    successes = int(row["successes"] or 0)
    fallback_known = int(row["fallback_known"] or 0)
    complete = int(row["cost_complete"] or 0)
    not_applicable = int(row["cost_na"] or 0)
    amounts = connection.execute(
        f"""SELECT cost_currency currency, CAST(SUM(known_cost_nanos) AS TEXT) amount,
          SUM(CASE WHEN cost_status='complete' THEN 1 ELSE 0 END) complete_requests,
          SUM(CASE WHEN cost_status='partial' THEN 1 ELSE 0 END) partial_requests,
          SUM(COALESCE(unknown_cost_attempts,0)) unknown_attempts
        FROM route_requests r WHERE {where} AND known_cost_nanos IS NOT NULL AND cost_currency IS NOT NULL
        GROUP BY cost_currency ORDER BY cost_currency""", values
    ).fetchall() if {"known_cost_nanos", "cost_currency"}.issubset(columns) else ()
    return {
        "requests": requests,
        "success": {"numerator": successes, "denominator": requests, "ratio": _ratio(successes, requests)},
        "fallback": {"numerator": int(row["fallbacks"] or 0), "denominator": fallback_known,
                     "unknown": int(row["fallback_unknown"] or 0),
                     "ratio": _ratio(int(row["fallbacks"] or 0), fallback_known)},
        "latency_ms": {"p50": _percentile(connection, where, values, .5),
                       "p95": _percentile(connection, where, values, .95)},
        "cost": {"coverage": {
            "complete": complete, "partial": int(row["cost_partial"] or 0),
            "unpriced": int(row["cost_unpriced"] or 0), "usage_missing": int(row["cost_missing"] or 0),
            "not_applicable": not_applicable, "unknown_invoked_attempts": int(row["unknown_attempts"] or 0),
            "numerator": complete, "denominator": requests - not_applicable,
            "ratio": _ratio(complete, requests - not_applicable)},
            "known_amounts": [{"currency": item["currency"], "known_amount_nanos": item["amount"],
                "complete_requests": item["complete_requests"], "partial_requests": item["partial_requests"],
                "unknown_invoked_attempts": item["unknown_attempts"]} for item in amounts]},
    }


def _series(connection: sqlite3.Connection, where: str, values: list[Any], columns: frozenset[str], attempt_columns: frozenset[str], width: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Aggregate observed UTC buckets and currency-separated known cost."""

    fallback = fallback_expression(columns, attempt_columns)
    rows = connection.execute(
        f"""SELECT CAST(strftime('%s', received_at) AS INTEGER)/? bucket,
          COUNT(*) requests, SUM(status='success') success, SUM(status='error') error,
          SUM(status='cancelled') cancelled, SUM(status='abandoned') abandoned,
          SUM(CASE WHEN {fallback} THEN 1 ELSE 0 END) fallback
        FROM route_requests r WHERE {where} GROUP BY bucket ORDER BY bucket""", (width, *values)
    ).fetchall()
    request_series = [{"bucket_start": datetime.fromtimestamp(row["bucket"] * width, UTC).isoformat().replace("+00:00", "Z"),
                       **{key: int(row[key] or 0) for key in ("requests", "success", "error", "cancelled", "abandoned", "fallback")}}
                      for row in rows]
    cost_series: list[dict[str, Any]] = []
    if {"known_cost_nanos", "cost_currency"}.issubset(columns):
        costs = connection.execute(
            f"""SELECT CAST(strftime('%s', received_at) AS INTEGER)/? bucket, cost_currency currency,
              CAST(SUM(known_cost_nanos) AS TEXT) amount FROM route_requests r
              WHERE {where} AND known_cost_nanos IS NOT NULL AND cost_currency IS NOT NULL
              GROUP BY bucket,cost_currency ORDER BY currency,bucket""", (width, *values)
        ).fetchall()
        cost_series = [{"bucket_start": datetime.fromtimestamp(row["bucket"] * width, UTC).isoformat().replace("+00:00", "Z"),
                        "currency": row["currency"], "known_amount_nanos": row["amount"]} for row in costs]
    return request_series, cost_series


def _breakdown(connection: sqlite3.Connection, where: str, values: list[Any], columns: frozenset[str], name: str) -> dict[str, Any]:
    """Return a bounded dimension breakdown with explicit truncation."""

    expression = column(columns, name, "'legacy_unknown'")
    rows = connection.execute(
        f"""SELECT COALESCE({expression},'legacy_unknown') key, COUNT(*) requests,
          SUM(status='success') successes, MAX(received_at) latest_received_at
        FROM route_requests r WHERE {where} GROUP BY key ORDER BY requests DESC,key LIMIT 101""", values
    ).fetchall()
    return {"items": [{"key": row["key"], "requests": row["requests"],
                       "success": {"numerator": row["successes"], "denominator": row["requests"],
                                   "ratio": _ratio(row["successes"], row["requests"])},
                       "latest_received_at": row["latest_received_at"],
                       "filter": {name: row["key"]}} for row in rows[:100]], "truncated": len(rows) > 100}


def _facets(connection: sqlite3.Connection, where: str, values: list[Any], columns: frozenset[str]) -> dict[str, Any]:
    """Return fixed bounded historical facet values without free-text search."""

    dimensions = {
        "protocols": "protocol", "endpoint_kinds": "endpoint_kind", "statuses": "status",
        "terminal_stages": "terminal_stage", "profiles": "profile", "models": "final_model",
        "policy_roles": "policy_role", "route_reasons": "route_reason",
    }
    result: dict[str, Any] = {}
    for key, name in dimensions.items():
        if name not in columns:
            result[key] = {"values": [], "truncated": False}
            continue
        rows = connection.execute(
            f"SELECT DISTINCT r.{name} value FROM route_requests r WHERE {where} "
            f"AND r.{name} IS NOT NULL ORDER BY value LIMIT 201", values
        ).fetchall()
        result[key] = {"values": [row["value"] for row in rows[:200]], "truncated": len(rows) > 200}
    return result


def _task_models(
    connection: sqlite3.Connection,
    where: str,
    values: list[Any],
    columns: frozenset[str],
    attempt_columns: frozenset[str],
) -> dict[str, Any]:
    """Aggregate only explicit task IDs with observed final models."""

    if "task_id" not in columns:
        return {"items": [], "truncated": False}
    fallback = fallback_expression(columns, attempt_columns)
    rows = connection.execute(
        f"""SELECT r.task_id,r.final_model,COUNT(*) requests,SUM(r.status='success') successes,
          SUM(CASE WHEN {fallback} THEN 1 ELSE 0 END) fallbacks,MAX(r.received_at) latest_received_at
        FROM route_requests r WHERE {where} AND r.task_id IS NOT NULL
        GROUP BY r.task_id,r.final_model ORDER BY latest_received_at DESC LIMIT 101""", values
    ).fetchall()
    return {"items": [{"task_id": row["task_id"], "final_model": row["final_model"],
                       "request_count": row["requests"],
                       "success": {"numerator": row["successes"], "denominator": row["requests"],
                                   "ratio": _ratio(row["successes"], row["requests"])},
                       "fallback": {"numerator": row["fallbacks"], "denominator": row["requests"],
                                    "unknown": 0, "ratio": _ratio(row["fallbacks"], row["requests"])},
                       "latest_received_at": row["latest_received_at"],
                       "filter": {"task_id": row["task_id"], "model": row["final_model"]}}
                      for row in rows[:100]], "truncated": len(rows) > 100}


def build_overview(
    connection: sqlite3.Connection,
    filters: DashboardFilters,
    capabilities: dict[str, frozenset[str]],
) -> dict[str, Any]:
    """Build all historical overview sections from one read transaction."""

    columns = capabilities["route_requests"]
    attempt_columns = capabilities.get("route_attempts", frozenset())
    where, values = filter_sql(filters, columns, attempt_columns)
    bucket_name, width = _bucket(filters)
    summary = _summary(connection, where, values, columns, attempt_columns)
    request_series, cost_series = _series(connection, where, values, columns, attempt_columns, width)
    freshness = connection.execute(
        f"SELECT MIN(received_at), MAX({column(columns, 'completed_at', 'received_at')}) FROM route_requests r"
    ).fetchone()
    breakdown_names = ("final_model", "profile", "policy_role", "route_reason")
    breakdowns = {f"{name}_breakdown": _breakdown(connection, where, values, columns, name) for name in breakdown_names}
    task_models = _task_models(connection, where, values, columns, attempt_columns)
    invoked = "a.upstream_invoked=1" if "upstream_invoked" in attempt_columns else "1"
    provider = connection.execute(
        f"""SELECT COALESCE(a.provider,'none') key, COUNT(*) requests FROM route_requests r
        LEFT JOIN route_attempts a ON a.request_id=r.request_id AND a.sequence=(SELECT MAX(sequence)
        FROM route_attempts x WHERE x.request_id=r.request_id AND {invoked.replace('a.', 'x.')})
        WHERE {where} GROUP BY key ORDER BY requests DESC,key LIMIT 101""", values
    ).fetchall() if attempt_columns else []
    failures = connection.execute(
        f"""SELECT r.request_id,r.received_at,{column(columns,'completed_at')} completed_at,r.protocol,
        {column(columns,'endpoint_kind', "'legacy_unknown'")} endpoint_kind,r.profile,r.status,
        {column(columns,'terminal_stage', "'legacy_unknown'")} terminal_stage,r.error_code,
        {column(columns,'primary_model', "'legacy_unknown'")} primary_model,
        {column(columns,'final_model', "'legacy_unknown'")} final_model,r.total_latency_ms
        FROM route_requests r WHERE {where} AND r.status<>'success'
        ORDER BY r.received_at DESC,r.request_id DESC LIMIT 20""", values
    ).fetchall()
    return {"range": {"start": filters.start, "end": filters.end, "bucket": bucket_name, "bucket_timezone": "UTC"},
            "freshness": {"earliest_available_at": freshness[0], "latest_completed_at": freshness[1]},
            "summary": summary, "request_series": request_series,
            "known_cost_series_by_currency": cost_series, **breakdowns,
            "model_breakdown": breakdowns["final_model_breakdown"],
            "policy_breakdown": breakdowns["policy_role_breakdown"],
            "task_model_breakdown": task_models,
            "facets": _facets(connection, where, values, columns),
            "provider_breakdown": {"items": [{"key": row["key"], "requests": row["requests"],
                                                "filter": {"provider": row["key"]}} for row in provider[:100]],
                                   "truncated": len(provider) > 100},
            "recent_failures": [dict(row) for row in failures]}
