"""Small application bootstrap for isolated dashboard dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import FastAPI

from llm_router.dashboard.http import register_dashboard_routes
from llm_router.dashboard.metrics import DashboardMetrics
from llm_router.dashboard.query import DashboardQuery
from llm_router.dashboard.runtime import RuntimeSnapshotSource
from llm_router.dashboard.sqlite_reader import (
    DashboardQueryExecutor,
    DashboardSQLiteReader,
)


@dataclass(frozen=True, slots=True)
class DashboardComponents:
    """Own dashboard resources that require application shutdown."""

    executor: DashboardQueryExecutor


def bootstrap_dashboard(app: FastAPI, runtime: object, started_at: datetime | None = None) -> DashboardComponents:
    """Build and register the enabled read-only dashboard."""

    config = runtime.config  # type: ignore[attr-defined]
    reader = DashboardSQLiteReader(config.storage.sqlite_path, config.dashboard.query_timeout_ms)
    query = DashboardQuery(reader, RuntimeSnapshotSource(runtime, started_at or datetime.now(UTC)))
    executor = DashboardQueryExecutor()
    metrics = DashboardMetrics(runtime.metrics.registry)  # type: ignore[attr-defined]
    register_dashboard_routes(app, query, executor, config.dashboard, runtime.client_key, metrics)  # type: ignore[attr-defined]
    return DashboardComponents(executor)
