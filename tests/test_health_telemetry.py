"""SQLite and Prometheus coverage for bounded health telemetry fields."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from llm_router.domain import AttemptEvent, FeatureSummary, RouteEvent
from llm_router.health.models import FailureClass, HealthState, HealthTransition
from llm_router.telemetry.metrics import RouterMetrics
from llm_router.telemetry.sqlite_store import SQLiteEventStore


def _event() -> RouteEvent:
    """Build one sanitized event containing a health skip."""

    attempt = AttemptEvent(
        request_id="req-1",
        sequence=1,
        provider="anthropic",
        model="anthropic_fast",
        started_at=datetime.now(timezone.utc),
        duration_ms=0.1,
        status="health_skipped",
    )
    return RouteEvent(
        request_id="req-1",
        received_at=datetime.now(timezone.utc),
        protocol="anthropic_messages",
        profile="code/auto",
        stream=False,
        feature_summary=FeatureSummary(("tools",), "small", "one", "none", "unknown", 0),
        primary_model="none",
        final_model="none",
        route_reason="health_no_available_target",
        policy_version="v2-test",
        status="error",
        attempt_count=0,
        total_latency_ms=1.0,
        error_code="router_no_available_target",
        attempts=(attempt,),
        health_enabled=True,
        health_snapshot_revision=4,
        health_filtered_count=2,
        health_skipped_count=1,
        health_reason="health_no_available_target",
    )


def test_sqlite_additive_health_columns_are_idempotent(tmp_path: Path) -> None:
    """Add health columns to a legacy database and persist bounded values."""

    path = tmp_path / "router.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE route_requests (request_id TEXT PRIMARY KEY, received_at TEXT NOT NULL, "
        "protocol TEXT NOT NULL, profile TEXT NOT NULL, stream INTEGER NOT NULL, "
        "feature_summary TEXT NOT NULL, primary_model TEXT NOT NULL, final_model TEXT NOT NULL, "
        "route_reason TEXT NOT NULL, policy_version TEXT NOT NULL, status TEXT NOT NULL, "
        "attempt_count INTEGER NOT NULL, time_to_first_event_ms REAL, "
        "total_latency_ms REAL NOT NULL, input_tokens INTEGER, output_tokens INTEGER, "
        "estimated_cost REAL, error_code TEXT, inbound_protocol TEXT, target_protocol TEXT, "
        "provider_account_scope TEXT, response_state_requested INTEGER NOT NULL DEFAULT 0, "
        "translation_mode TEXT NOT NULL DEFAULT 'none')"
    )
    connection.commit()
    connection.close()
    event = _event()
    store = SQLiteEventStore(str(path))

    async def write() -> None:
        """Start, migrate, append, and close one event store."""

        await store.start()
        await store.append(event)
        await store.close()

    asyncio.run(write())
    connection = sqlite3.connect(path)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(route_requests)")}
    values = connection.execute(
        "SELECT health_enabled, health_snapshot_revision, health_filtered_count, "
        "health_skipped_count, health_reason FROM route_requests WHERE request_id='req-1'"
    ).fetchone()
    connection.close()

    assert {
        "health_enabled",
        "health_snapshot_revision",
        "health_filtered_count",
        "health_skipped_count",
        "health_reason",
    }.issubset(columns)
    assert values == (1, 4, 2, 1, "health_no_available_target")


def test_metrics_expose_bounded_health_dimensions() -> None:
    """Expose health gauges and counters without request content labels."""

    metrics = RouterMetrics()
    metrics.record_health_transition(
        HealthTransition(
            provider="anthropic",
            target_alias="anthropic_fast",
            from_state=HealthState.HEALTHY,
            to_state=HealthState.COOLDOWN,
            failure_class=FailureClass.TARGET_TRANSIENT,
            cooldown_seconds=30,
        ),
        "anthropic_messages",
    )
    metrics.record(_event())
    rendered = metrics.render().decode()

    assert "llm_router_health_state" in rendered
    assert "llm_router_health_skipped_total" in rendered
    assert "llm_router_no_available_target_total" in rendered
    assert "anthropic_fast" in rendered
    assert "request body" not in rendered
