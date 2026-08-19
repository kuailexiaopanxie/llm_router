"""SQLite and Prometheus coverage for bounded health telemetry fields."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from llm_router.domain import AttemptEvent, Protocol
from llm_router.health.models import FailureClass, HealthState, HealthTransition
from llm_router.observability.metrics import RouterMetrics
from llm_router.observability.models import (
    CostEstimate,
    CostStatus,
    EndpointKind,
    ExecutionObservation,
    ObservationBundle,
    RequestStatus,
    RouteObservation,
    RoutingObservation,
    TerminalStage,
    UsageBreakdown,
)
from llm_router.observability.sqlite_store import SQLiteObservationStore
from llm_router.observability.tracing import trace_context


def _event() -> ObservationBundle:
    """Build one immutable observation bundle containing a health skip."""

    request_id = uuid4()
    now = datetime.now(timezone.utc)
    attempt = AttemptEvent(
        request_id=str(request_id),
        sequence=1,
        provider="anthropic",
        model="anthropic_fast",
        started_at=datetime.now(timezone.utc),
        duration_ms=0.1,
        status="health_skipped",
        upstream_invoked=False,
    )
    routing = RoutingObservation(
        "code/auto",
        None,
        now,
        0.2,
        None,
        (),
        "v2-test",
        "a" * 64,
        "control",
        None,
        "health_no_available_target",
        (),
        4,
        2,
        "health_no_available_target",
        "error",
    )
    execution = ExecutionObservation(
        now,
        0.1,
        None,
        None,
        None,
        0,
        1,
        False,
        "error",
        (attempt,),
    )
    observation = RouteObservation(
        request_id,
        None,
        trace_context(None),
        now,
        now,
        EndpointKind.MESSAGES,
        Protocol.ANTHROPIC_MESSAGES,
        "code/auto",
        False,
        TerminalStage.EXECUTION_PRE_COMMIT,
        RequestStatus.ERROR,
        routing,
        execution,
        UsageBreakdown.missing(),
        "router_no_available_target",
    )
    return ObservationBundle(
        observation,
        CostEstimate(CostStatus.USAGE_MISSING, None, None, None),
        (),
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
    store = SQLiteObservationStore(str(path))

    async def write() -> None:
        """Start, migrate, append, and close one event store."""

        await store.start()
        assert await store.append(event) == "written"
        await store.close()

    asyncio.run(write())
    connection = sqlite3.connect(path)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(route_requests)")}
    values = connection.execute(
        "SELECT health_enabled, health_snapshot_revision, health_filtered_count, "
        "health_skipped_count, health_reason, policy_hash, policy_role, effective_profile "
        "FROM route_requests WHERE request_id=?",
        (str(event.observation.request_id),),
    ).fetchone()
    connection.close()

    assert {
        "health_enabled",
        "health_snapshot_revision",
        "health_filtered_count",
        "health_skipped_count",
        "health_reason",
    }.issubset(columns)
    assert values == (
        1,
        4,
        2,
        1,
        "health_no_available_target",
        "a" * 64,
        "control",
        None,
    )


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
    metrics.record_observation(_event())
    rendered = metrics.render().decode()

    assert "llm_router_health_state" in rendered
    assert "llm_router_health_skipped_total" in rendered
    assert "llm_router_no_available_target_total" in rendered
    assert "anthropic_fast" in rendered
    assert "request body" not in rendered
