"""Atomic additive SQLite persistence for terminal observation bundles."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import aiosqlite

from llm_router.observability.models import ObservationBundle

_NEW_COLUMNS = {
    "task_id": "TEXT",
    "inbound_protocol": "TEXT",
    "target_protocol": "TEXT",
    "provider_account_scope": "TEXT",
    "response_state_requested": "INTEGER NOT NULL DEFAULT 0",
    "translation_mode": "TEXT NOT NULL DEFAULT 'none'",
    "health_enabled": "INTEGER NOT NULL DEFAULT 0",
    "health_snapshot_revision": "INTEGER NOT NULL DEFAULT 0",
    "health_filtered_count": "INTEGER NOT NULL DEFAULT 0",
    "health_skipped_count": "INTEGER NOT NULL DEFAULT 0",
    "health_reason": "TEXT",
    "trace_id": "TEXT",
    "root_span_id": "TEXT",
    "trace_source": "TEXT",
    "trace_captured": "INTEGER NOT NULL DEFAULT 0",
    "completed_at": "TEXT",
    "endpoint_kind": "TEXT",
    "terminal_stage": "TEXT",
    "routing_duration_ms": "REAL",
    "usage_status": "TEXT",
    "cost_status": "TEXT",
    "cost_currency": "TEXT",
    "known_cost_nanos": "INTEGER",
    "pricing_id": "TEXT",
    "unknown_cost_attempts": "INTEGER NOT NULL DEFAULT 0",
    "effective_profile": "TEXT",
    "policy_hash": "TEXT",
    "policy_role": "TEXT",
    "assignment_reason": "TEXT",
}


class ObservationStoreError(RuntimeError):
    """Represent a bounded durable observation failure."""


class SQLiteObservationStore:
    """Persist request, attempt, usage, cost, and spans in one transaction."""

    def __init__(self, path: str) -> None:
        """Bind a local SQLite path without opening it."""

        self._path = Path(path).expanduser()
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Create legacy-compatible tables and apply additive migrations."""

        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(self._path, check_same_thread=False)
        try:
            await connection.execute("PRAGMA journal_mode=WAL")
            await connection.execute("PRAGMA synchronous=NORMAL")
            await connection.execute("PRAGMA busy_timeout=2000")
            await connection.executescript(_BASE_SCHEMA)
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute("PRAGMA table_info(route_requests)")
            existing = {row[1] for row in await cursor.fetchall()}
            await cursor.close()
            for name, definition in _NEW_COLUMNS.items():
                if name not in existing:
                    await connection.execute(
                        f"ALTER TABLE route_requests ADD COLUMN {name} {definition}"
                    )
            cursor = await connection.execute("PRAGMA table_info(route_attempts)")
            attempts = {row[1] for row in await cursor.fetchall()}
            await cursor.close()
            if "upstream_invoked" not in attempts:
                await connection.execute(
                    "ALTER TABLE route_attempts ADD COLUMN upstream_invoked INTEGER NOT NULL DEFAULT 1"
                )
                await connection.execute(
                    "UPDATE route_attempts SET upstream_invoked = 0 WHERE status = 'health_skipped'"
                )
            for statement in _INDEXES:
                await connection.execute(statement)
            await connection.commit()
        except Exception:
            await connection.rollback()
            await connection.close()
            raise
        self._connection = connection

    async def append(self, bundle: ObservationBundle) -> str:
        """Atomically append an immutable observation or retain its duplicate."""

        if self._connection is None:
            raise ObservationStoreError("observation store is unavailable")
        async with self._lock:
            try:
                await self._connection.execute("BEGIN IMMEDIATE")
                cursor = await self._connection.execute(
                    "SELECT 1 FROM route_requests WHERE request_id = ?",
                    (str(bundle.observation.request_id),),
                )
                duplicate = await cursor.fetchone() is not None
                await cursor.close()
                if duplicate:
                    await self._connection.rollback()
                    return "duplicate"
                await self._insert_request(bundle)
                await self._insert_children(bundle)
                await self._connection.commit()
                return "written"
            except (aiosqlite.Error, TypeError, ValueError, OverflowError) as exc:
                await self._connection.rollback()
                raise ObservationStoreError("observation bundle could not be persisted") from exc

    async def _insert_request(self, bundle: ObservationBundle) -> None:
        """Insert one v0.7 row while satisfying legacy NOT NULL columns."""

        assert self._connection is not None
        event = bundle.observation
        routing = event.routing
        execution = event.execution
        primary = routing.primary_model if routing and routing.primary_model else "none"
        final = execution.final_target if execution and execution.final_target else "none"
        route_reason = routing.route_reason if routing and routing.route_reason else event.terminal_stage.value
        profile = event.profile or "unknown"
        protocol = event.protocol.value if event.protocol else "unknown"
        duration_ms = (event.completed_at - event.received_at).total_seconds() * 1000
        estimated = (
            bundle.cost.known_amount_nanos / 1_000_000_000
            if bundle.cost.currency == "USD" and bundle.cost.known_amount_nanos is not None
            else None
        )
        await self._connection.execute(
            """
            INSERT INTO route_requests
            (request_id, task_id, received_at, protocol, profile, stream, feature_summary,
             primary_model, final_model, route_reason, policy_version, status, attempt_count,
             time_to_first_event_ms, total_latency_ms, input_tokens, output_tokens,
             estimated_cost, error_code, trace_id, root_span_id, trace_source, trace_captured,
             completed_at, endpoint_kind, terminal_stage, routing_duration_ms, usage_status,
             cost_status, cost_currency, known_cost_nanos, pricing_id, unknown_cost_attempts,
             inbound_protocol, target_protocol, health_enabled, health_snapshot_revision,
             health_filtered_count, health_skipped_count, health_reason, effective_profile,
             policy_hash, policy_role, assignment_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(event.request_id),
                str(event.task_id) if event.task_id else None,
                event.received_at.isoformat(),
                protocol,
                profile,
                int(event.stream),
                _UNKNOWN_FEATURES,
                primary,
                final,
                route_reason,
                routing.policy_version if routing else "unknown",
                event.status.value,
                execution.attempt_count if execution else 0,
                execution.time_to_first_event_ms if execution else None,
                duration_ms,
                event.usage.input_uncached_tokens,
                event.usage.output_tokens,
                estimated,
                event.error_code,
                event.trace_context.trace_id,
                event.trace_context.root_span_id,
                event.trace_context.source,
                int(bool(bundle.spans)),
                event.completed_at.isoformat(),
                event.endpoint_kind.value,
                event.terminal_stage.value,
                routing.duration_ms if routing else None,
                event.usage.status.value,
                bundle.cost.status.value,
                bundle.cost.currency,
                bundle.cost.known_amount_nanos,
                bundle.cost.pricing_id,
                bundle.cost.unknown_invoked_attempts,
                protocol,
                protocol,
                1,
                routing.health_snapshot_revision if routing else 0,
                routing.health_filtered_count if routing else 0,
                execution.health_skipped_count if execution else 0,
                routing.health_reason if routing else None,
                routing.effective_profile if routing else None,
                routing.policy_hash if routing else None,
                routing.policy_role if routing else None,
                routing.assignment_reason if routing else None,
            ),
        )

    async def _insert_children(self, bundle: ObservationBundle) -> None:
        """Insert ordered attempts, known usage, cost snapshots, and local spans."""

        assert self._connection is not None
        event = bundle.observation
        attempts = event.execution.attempts if event.execution else ()
        await self._connection.executemany(
            """
            INSERT INTO route_attempts
            (request_id, sequence, provider, model, started_at, duration_ms, status,
             http_status, error_code, upstream_invoked)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    attempt.request_id,
                    attempt.sequence,
                    attempt.provider,
                    attempt.model,
                    attempt.started_at.isoformat(),
                    attempt.duration_ms,
                    attempt.status,
                    attempt.http_status,
                    attempt.error_code,
                    int(attempt.upstream_invoked),
                )
                for attempt in attempts
            ],
        )
        usage = {
            "input_uncached": event.usage.input_uncached_tokens,
            "input_cache_read": event.usage.input_cache_read_tokens,
            "input_cache_write": event.usage.input_cache_write_tokens,
            "output": event.usage.output_tokens,
            "reasoning_output": event.usage.reasoning_output_tokens,
        }
        await self._connection.executemany(
            "INSERT INTO route_usage (request_id, kind, tokens) VALUES (?, ?, ?)",
            [(str(event.request_id), kind, tokens) for kind, tokens in usage.items() if tokens is not None],
        )
        await self._connection.executemany(
            """
            INSERT INTO route_cost_items
            (request_id, kind, tokens, rate_per_million, amount_nanos) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (str(event.request_id), item.kind, item.tokens, item.rate_per_million, item.amount_nanos)
                for item in bundle.cost.line_items
            ],
        )
        await self._connection.executemany(
            """
            INSERT INTO route_spans
            (trace_id, span_id, parent_span_id, request_id, name, started_at,
             duration_ms, status, attributes_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    span.trace_id,
                    span.span_id,
                    span.parent_span_id,
                    str(span.request_id),
                    span.name,
                    span.started_at.isoformat(),
                    span.duration_ms,
                    span.status,
                    json.dumps(dict(span.attributes), sort_keys=True, separators=(",", ":")),
                )
                for span in bundle.spans
            ],
        )

    async def close(self) -> None:
        """Close the observation connection."""

        if self._connection is not None:
            await self._connection.close()
            self._connection = None


_UNKNOWN_FEATURES = json.dumps(
    {
        "required_capabilities": [],
        "input_size_bucket": "unknown",
        "message_count_bucket": "unknown",
        "tool_rounds_bucket": "unknown",
        "outcome_signal": "unknown",
        "task_signal_count": 0,
    },
    sort_keys=True,
    separators=(",", ":"),
)

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_route_received ON route_requests(received_at)",
    "CREATE INDEX IF NOT EXISTS idx_route_task_received ON route_requests(task_id, received_at)",
    "CREATE INDEX IF NOT EXISTS idx_route_trace ON route_requests(trace_id)",
    "CREATE INDEX IF NOT EXISTS idx_route_final_received ON route_requests(final_model, received_at)",
    "CREATE INDEX IF NOT EXISTS idx_route_status_received ON route_requests(status, received_at)",
    "CREATE INDEX IF NOT EXISTS idx_span_request_started ON route_spans(request_id, started_at)",
)


_BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS route_requests (
 request_id TEXT PRIMARY KEY, task_id TEXT, received_at TEXT NOT NULL, protocol TEXT NOT NULL,
 profile TEXT NOT NULL, stream INTEGER NOT NULL, feature_summary TEXT NOT NULL,
 primary_model TEXT NOT NULL, final_model TEXT NOT NULL, route_reason TEXT NOT NULL,
 policy_version TEXT NOT NULL, status TEXT NOT NULL, attempt_count INTEGER NOT NULL,
 time_to_first_event_ms REAL, total_latency_ms REAL NOT NULL, input_tokens INTEGER,
 output_tokens INTEGER, estimated_cost REAL, error_code TEXT
);
CREATE TABLE IF NOT EXISTS route_attempts (
 request_id TEXT NOT NULL, sequence INTEGER NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
 started_at TEXT NOT NULL, duration_ms REAL NOT NULL, status TEXT NOT NULL, http_status INTEGER,
 error_code TEXT, PRIMARY KEY (request_id, sequence)
);
CREATE TABLE IF NOT EXISTS route_usage (
 request_id TEXT NOT NULL, kind TEXT NOT NULL, tokens INTEGER NOT NULL,
 PRIMARY KEY (request_id, kind)
);
CREATE TABLE IF NOT EXISTS route_cost_items (
 request_id TEXT NOT NULL, kind TEXT NOT NULL, tokens INTEGER NOT NULL,
 rate_per_million TEXT NOT NULL, amount_nanos INTEGER NOT NULL, PRIMARY KEY (request_id, kind)
);
CREATE TABLE IF NOT EXISTS route_spans (
 trace_id TEXT NOT NULL, span_id TEXT NOT NULL, parent_span_id TEXT, request_id TEXT NOT NULL,
 name TEXT NOT NULL, started_at TEXT NOT NULL, duration_ms REAL NOT NULL, status TEXT NOT NULL,
 attributes_json TEXT NOT NULL, PRIMARY KEY (trace_id, span_id)
);
"""
