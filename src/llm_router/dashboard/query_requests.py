"""Bounded Requests page and Request Detail SQLite readers."""

from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import datetime
from typing import Any
from uuid import UUID

from llm_router.dashboard.cursor import encode_cursor
from llm_router.dashboard.models import RequestCursor, RequestPageQuery
from llm_router.dashboard.query_sql import column, fallback_expression, filter_sql

_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID = re.compile(r"^[0-9a-f]{16}$")
_TRACE_ATTRIBUTE_KEYS = {
    "llm_router.request_id", "llm_router.endpoint_kind", "llm_router.protocol",
    "llm_router.profile", "llm_router.terminal_stage", "llm_router.usage_status",
    "llm_router.cost_status", "llm_router.error_code", "llm_router.policy_version",
    "llm_router.policy_role", "llm_router.route_reason", "llm_router.upstream_invoked",
    "gen_ai.operation.name", "gen_ai.provider.name", "gen_ai.response.model",
    "gen_ai.request.model",
}


def _request_select(columns: frozenset[str]) -> str:
    """Build a fixed legacy-compatible request projection."""

    names = (
        "task_id", "completed_at", "endpoint_kind", "terminal_stage", "effective_profile",
        "policy_hash", "policy_role", "assignment_reason", "routing_duration_ms", "trace_id",
        "trace_captured", "usage_status", "cost_status", "cost_currency", "known_cost_nanos",
        "pricing_id", "unknown_cost_attempts", "health_snapshot_revision", "health_filtered_count",
        "health_skipped_count", "health_reason",
    )
    defaults = {"endpoint_kind": "'legacy_unknown'", "terminal_stage": "'legacy_unknown'",
                "usage_status": "'legacy_unknown'", "cost_status": "'legacy_unknown'",
                "trace_captured": "0", "unknown_cost_attempts": "0", "health_snapshot_revision": "0",
                "health_filtered_count": "0", "health_skipped_count": "0"}
    return ",".join(f"{column(columns, name, defaults.get(name, 'NULL'))} AS {name}" for name in names)


def _providers(
    connection: sqlite3.Connection,
    request_ids: list[str],
    attempt_columns: frozenset[str],
) -> dict[str, str]:
    """Resolve actual final providers from invoked attempts only."""

    if not request_ids or not attempt_columns:
        return {}
    invoked = "a.upstream_invoked=1" if "upstream_invoked" in attempt_columns else "1"
    placeholders = ",".join("?" for _ in request_ids)
    rows = connection.execute(
        f"""SELECT a.request_id,a.provider FROM route_attempts a WHERE a.request_id IN ({placeholders})
        AND {invoked} AND a.sequence=(SELECT MAX(x.sequence) FROM route_attempts x
        WHERE x.request_id=a.request_id AND {invoked.replace('a.', 'x.')})""", request_ids
    ).fetchall()
    return {row["request_id"]: row["provider"] for row in rows}


def build_request_page(
    connection: sqlite3.Connection,
    query: RequestPageQuery,
    capabilities: dict[str, frozenset[str]],
) -> dict[str, Any]:
    """Read one stable keyset page without an expensive total count."""

    columns = capabilities["route_requests"]
    attempt_columns = capabilities.get("route_attempts", frozenset())
    where, values = filter_sql(query.filters, columns, attempt_columns)
    if query.cursor is not None:
        where += " AND (r.received_at < ? OR (r.received_at = ? AND r.request_id < ?))"
        cursor_time = query.cursor.before_time.isoformat()
        values.extend((cursor_time, cursor_time, str(query.cursor.before_request)))
    rows = connection.execute(
        f"""SELECT r.request_id,r.received_at,r.protocol,r.profile,r.stream,r.primary_model,
        r.final_model,r.route_reason,r.policy_version,r.status,r.attempt_count,r.total_latency_ms,
        r.input_tokens,r.output_tokens,r.error_code,{_request_select(columns)},
        {fallback_expression(columns, attempt_columns)} AS fallback_used,
        {("(SELECT COUNT(*) FROM route_attempts ai WHERE ai.request_id=r.request_id AND " + ("ai.upstream_invoked=1" if "upstream_invoked" in attempt_columns else "1") + ")") if attempt_columns else "0"} upstream_invoked_count
        FROM route_requests r WHERE {where} ORDER BY r.received_at DESC,r.request_id DESC LIMIT ?""",
        (*values, query.limit + 1),
    ).fetchall()
    has_more = len(rows) > query.limit
    rows = rows[:query.limit]
    providers = _providers(connection, [row["request_id"] for row in rows], attempt_columns)
    items: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["final_provider"] = providers.get(row["request_id"], "none")
        item["fallback_used"] = bool(item["fallback_used"])
        if item.get("known_cost_nanos") is not None:
            item["known_cost_nanos"] = str(item["known_cost_nanos"])
        items.append(item)
    next_cursor = None
    if has_more and rows:
        next_cursor = encode_cursor(RequestCursor(datetime.fromisoformat(rows[-1]["received_at"]), UUID(rows[-1]["request_id"])))
    return {"items": items, "next_cursor": next_cursor, "has_more": has_more}


def _attempts(connection: sqlite3.Connection, request_id: str, capabilities: dict[str, frozenset[str]]) -> dict[str, Any]:
    """Read ordered bounded attempts and preserve upstream invocation facts."""

    columns = capabilities.get("route_attempts")
    if not columns:
        return {"attempts": [], "gap": "legacy_unknown", "truncated": False}
    invoked = "upstream_invoked" if "upstream_invoked" in columns else "1 AS upstream_invoked"
    rows = connection.execute(
        f"""SELECT sequence,provider,model,started_at,duration_ms,status,http_status,error_code,{invoked}
        FROM route_attempts WHERE request_id=? ORDER BY sequence LIMIT 101""", (request_id,)
    ).fetchall()
    return {"attempts": [{**dict(row), "upstream_invoked": bool(row["upstream_invoked"])} for row in rows[:100]],
            "gap": None, "truncated": len(rows) > 100}


def _usage(connection: sqlite3.Connection, request_id: str, capabilities: dict[str, frozenset[str]], row: sqlite3.Row) -> dict[str, Any]:
    """Read fixed-order normalized usage with a legacy fallback."""

    order = {name: index for index, name in enumerate(("input_uncached", "input_cache_read", "input_cache_write", "output", "reasoning_output"))}
    if capabilities.get("route_usage"):
        rows = connection.execute("SELECT kind,tokens FROM route_usage WHERE request_id=?", (request_id,)).fetchall()
        items = sorted((dict(item) for item in rows), key=lambda item: order.get(item["kind"], 99))
    else:
        items = [{"kind": "input_uncached", "tokens": row["input_tokens"]},
                 {"kind": "output", "tokens": row["output_tokens"]}]
        items = [item for item in items if item["tokens"] is not None]
    return {"status": row["usage_status"], "items": items,
            "gap": "usage_missing" if not items else None}


def _cost(connection: sqlite3.Connection, request_id: str, capabilities: dict[str, frozenset[str]], row: sqlite3.Row) -> dict[str, Any]:
    """Read immutable pricing snapshots without recalculating historical prices."""

    rows = connection.execute(
        "SELECT kind,tokens,rate_per_million,amount_nanos FROM route_cost_items WHERE request_id=? ORDER BY kind LIMIT 101",
        (request_id,),
    ).fetchall() if capabilities.get("route_cost_items") else ()
    amount = row["known_cost_nanos"]
    return {"status": row["cost_status"], "currency": row["cost_currency"],
            "known_amount_nanos": str(amount) if amount is not None else None,
            "pricing_id": row["pricing_id"], "unknown_invoked_attempts": row["unknown_cost_attempts"],
            "items": [{**dict(item), "amount_nanos": str(item["amount_nanos"])} for item in rows[:100]],
            "truncated": len(rows) > 100, "gap": "cost_unpriced" if row["cost_status"] == "unpriced" else None}


def _trace(connection: sqlite3.Connection, request_id: str, capabilities: dict[str, frozenset[str]], row: sqlite3.Row) -> dict[str, Any]:
    """Validate and return bounded whitelist-only local trace spans."""

    if not capabilities.get("route_spans") or not row["trace_captured"]:
        return {"gap": "trace_not_captured", "spans": [], "truncated": False}
    rows = connection.execute(
        "SELECT trace_id,span_id,parent_span_id,name,started_at,duration_ms,status,attributes_json "
        "FROM route_spans WHERE request_id=? ORDER BY started_at,span_id LIMIT 201", (request_id,)
    ).fetchall()
    spans: list[dict[str, Any]] = []
    gap = None
    ids = {item["span_id"] for item in rows}
    for item in rows[:200]:
        try:
            attributes = json.loads(item["attributes_json"])
            if not isinstance(attributes, dict) or set(attributes) - _TRACE_ATTRIBUTE_KEYS:
                raise ValueError
        except (ValueError, TypeError, json.JSONDecodeError):
            gap = "trace_attributes_invalid"
            attributes = {}
        duration = item["duration_ms"]
        if (not _TRACE_ID.fullmatch(item["trace_id"]) or item["trace_id"] != row["trace_id"]
                or not _SPAN_ID.fullmatch(item["span_id"]) or not math.isfinite(duration) or duration < 0):
            gap = "trace_integrity_gap"
        if item["parent_span_id"] and item["parent_span_id"] not in ids:
            gap = "trace_integrity_gap"
        span = dict(item)
        span.pop("attributes_json")
        span["attributes"] = attributes
        spans.append(span)
    return {"gap": gap, "spans": spans, "truncated": len(rows) > 200}


def _outcome(connection: sqlite3.Connection, request_id: str, capabilities: dict[str, frozenset[str]]) -> dict[str, Any]:
    """Read optional bounded Outcome facts and identify conflicting verdicts."""

    if not capabilities.get("outcome_events"):
        return {"status": "not_observed", "events": [], "gap": "outcome_not_observed"}
    rows = connection.execute(
        "SELECT event_id,verdict,evidence,source,observed_at FROM outcome_events "
        "WHERE request_id=? ORDER BY observed_at,event_id LIMIT 101", (request_id,),
    ).fetchall()
    verdicts = {row["verdict"] for row in rows}
    return {"status": "observed" if rows else "not_observed", "events": [dict(row) for row in rows[:100]],
            "conflict": len(verdicts) > 1, "truncated": len(rows) > 100,
            "gap": None if rows else "outcome_not_observed"}


def build_request_detail(connection: sqlite3.Connection, request_id: UUID, capabilities: dict[str, frozenset[str]]) -> dict[str, Any] | None:
    """Read one internally consistent request detail snapshot."""

    columns = capabilities["route_requests"]
    row = connection.execute(
        f"""SELECT r.request_id,r.received_at,r.protocol,r.profile,r.stream,r.primary_model,r.final_model,
        r.route_reason,r.policy_version,r.status,r.attempt_count,r.total_latency_ms,r.input_tokens,
        r.output_tokens,r.error_code,{_request_select(columns)} FROM route_requests r WHERE request_id=?""",
        (str(request_id),),
    ).fetchone()
    if row is None:
        return None
    execution = _attempts(connection, str(request_id), capabilities)
    usage = _usage(connection, str(request_id), capabilities, row)
    cost = _cost(connection, str(request_id), capabilities, row)
    trace = _trace(connection, str(request_id), capabilities, row)
    outcome = _outcome(connection, str(request_id), capabilities)
    attempts = execution["attempts"]
    final_provider = next((item["provider"] for item in reversed(attempts) if item["upstream_invoked"]), "none")
    request = {key: row[key] for key in ("request_id", "task_id", "trace_id", "received_at", "completed_at",
        "protocol", "endpoint_kind", "stream", "status", "terminal_stage", "error_code", "total_latency_ms")}
    routing = {key: row[key] for key in ("profile", "effective_profile", "policy_version", "policy_hash", "policy_role",
        "assignment_reason", "route_reason", "primary_model", "final_model", "routing_duration_ms",
        "health_snapshot_revision", "health_filtered_count", "health_skipped_count", "health_reason")}
    routing["final_provider"] = final_provider
    routing["fallback_used"] = bool(row["primary_model"] not in ("none", "unknown") and row["final_model"] != row["primary_model"])
    gaps = list(dict.fromkeys(section["gap"] for section in (usage, cost, trace, outcome) if section.get("gap")))
    decision = capabilities.get("route_decision_inputs")
    if not decision or not connection.execute("SELECT 1 FROM route_decision_inputs WHERE request_id=?", (str(request_id),)).fetchone():
        gaps.append("decision_not_captured")
    return {"request": request, "routing": routing, "execution": execution,
            "usage": usage, "cost": cost, "trace": trace, "outcome": outcome, "gaps": gaps}
