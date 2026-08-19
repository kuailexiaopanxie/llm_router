"""v0.7 OTLP degradation and bounded retention tests."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import aiosqlite

from llm_router.observability.config import OtlpConfig
from llm_router.observability.metrics import RouterMetrics
from llm_router.observability.models import (
    EndpointKind,
    RequestStatus,
    RouteObservation,
    TerminalStage,
    UsageBreakdown,
)
from llm_router.observability.otlp import OtlpTraceExporter
from llm_router.observability.retention import RetentionWorker
from llm_router.observability.sqlite_store import SQLiteObservationStore
from llm_router.observability.tracing import TraceBuilder, trace_context


def test_otlp_missing_sdk_is_fail_open() -> None:
    """Keep startup active when the optional SDK cannot be loaded."""

    metrics = RouterMetrics()

    async def exercise() -> None:
        """Start and close an unavailable exporter."""

        exporter = OtlpTraceExporter(
            OtlpConfig(enabled=True),
            metrics,
            exporter_factory=lambda: (_ for _ in ()).throw(ModuleNotFoundError()),
        )
        await exporter.start()
        exporter.record(())
        await exporter.close()

    asyncio.run(exercise())
    assert 'llm_router_trace_export_total{exporter="otlp",status="unavailable"} 1.0' in (
        metrics.render().decode()
    )


def test_otlp_exports_a_bounded_fake_batch() -> None:
    """Export mapped neutral spans through a fake SDK exporter without network."""

    exported: list[object] = []

    def export(spans: object) -> SimpleNamespace:
        """Capture one exporter batch and report SDK-style success."""

        exported.append(spans)
        return SimpleNamespace(name="SUCCESS")

    now = datetime.now(UTC)
    event = RouteObservation(
        uuid4(),
        None,
        trace_context(None),
        now,
        now,
        EndpointKind.MESSAGES,
        None,
        None,
        False,
        TerminalStage.AUTHENTICATION,
        RequestStatus.ERROR,
        None,
        None,
        UsageBreakdown.not_applicable(),
        "router_unauthorized",
    )
    spans = TraceBuilder(Decimal(1)).build(event)
    metrics = RouterMetrics()
    fake = SimpleNamespace(export=export, shutdown=lambda: None)

    async def exercise() -> None:
        """Start, enqueue, and drain one fake exporter."""

        exporter = OtlpTraceExporter(
            OtlpConfig(enabled=True, batch_size=1),
            metrics,
            exporter_factory=lambda: fake,
            span_mapper=lambda span: span,
        )
        await exporter.start()
        exporter.record(spans)
        await exporter.close()

    asyncio.run(exercise())
    assert exported and tuple(exported[0]) == spans  # type: ignore[arg-type]
    assert 'llm_router_trace_export_total{exporter="otlp",status="success"} 1.0' in (
        metrics.render().decode()
    )


def test_retention_deletes_only_observation_tables(tmp_path: Path) -> None:
    """Delete at most the expired observation rows and retain evaluation data."""

    path = tmp_path / "router.db"

    async def prepare_and_retain() -> int:
        """Create schema, seed old rows, and run one bounded batch."""

        store = SQLiteObservationStore(str(path))
        await store.start()
        await store.close()
        old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        async with aiosqlite.connect(path) as connection:
            await connection.execute("CREATE TABLE evaluation_sentinel (value TEXT)")
            await connection.execute("INSERT INTO evaluation_sentinel VALUES ('keep')")
            await connection.execute(
                """
                INSERT INTO route_requests
                (request_id, received_at, protocol, profile, stream, feature_summary,
                 primary_model, final_model, route_reason, policy_version, status,
                 attempt_count, total_latency_ms)
                VALUES ('old-request', ?, 'unknown', 'unknown', 0, '{}',
                        'none', 'none', 'authentication', 'unknown', 'error', 0, 1)
                """,
                (old,),
            )
            await connection.commit()
        worker = RetentionWorker(str(path), 7, RouterMetrics())
        return await worker.run_batch(datetime.now(UTC))

    assert asyncio.run(prepare_and_retain()) == 1
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM route_requests").fetchone() == (0,)
        assert connection.execute("SELECT value FROM evaluation_sentinel").fetchone() == ("keep",)
