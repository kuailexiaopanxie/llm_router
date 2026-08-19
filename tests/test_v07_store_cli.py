"""v0.7 atomic observation storage and read-only CLI tests."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from llm_router.observability.cli import run_cost, run_routes, run_trace
from llm_router.observability.models import (
    CostEstimate,
    CostStatus,
    EndpointKind,
    ObservationBundle,
    RequestStatus,
    RouteObservation,
    TerminalStage,
    UsageBreakdown,
)
from llm_router.observability.sqlite_store import SQLiteObservationStore
from llm_router.observability.tracing import TraceBuilder, trace_context


def _bundle() -> ObservationBundle:
    """Build one early terminal bundle with a captured local root span."""

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
    cost = CostEstimate(CostStatus.NOT_APPLICABLE, None, None, None)
    return ObservationBundle(event, cost, TraceBuilder(Decimal(1)).build(event, cost))


def test_store_is_atomic_and_duplicate_preserves_first(tmp_path: Path) -> None:
    """Write all children once and retain the first immutable terminal fact."""

    path = tmp_path / "router.db"
    bundle = _bundle()

    async def write() -> None:
        """Start, append twice, and close one observation store."""

        store = SQLiteObservationStore(str(path))
        await store.start()
        assert await store.append(bundle) == "written"
        assert await store.append(bundle) == "duplicate"
        await store.close()

    asyncio.run(write())
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM route_requests").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM route_spans").fetchone() == (1,)


def test_observation_cli_is_read_only_and_reports_coverage(
    tmp_path: Path, capsys: object
) -> None:
    """Run route, trace, and cost queries without changing database bytes."""

    path = tmp_path / "router.db"
    bundle = _bundle()

    async def prepare() -> None:
        """Persist one query fixture."""

        store = SQLiteObservationStore(str(path))
        await store.start()
        await store.append(bundle)
        await store.close()

    asyncio.run(prepare())
    before = path.read_bytes()
    assert run_routes(["--db", str(path), "--format", "json"]) == 0
    routes = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert routes["rows"][0]["cost_status"] == "not_applicable"
    assert run_trace(
        ["--db", str(path), "--request", str(bundle.observation.request_id), "--format", "json"]
    ) == 0
    trace = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert trace["trace"]["gap"] is None
    assert run_cost(["--db", str(path), "--format", "json"]) == 0
    cost = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert cost["groups"][0]["cost_missing"] == 1
    assert path.read_bytes() == before
