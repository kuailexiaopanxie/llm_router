"""Dashboard HTTP registration, auth, and asset tests."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_v08_dashboard_overview import _fixture

from llm_router.dashboard.config import DashboardConfig
from llm_router.dashboard.http import register_dashboard_routes
from llm_router.dashboard.metrics import DashboardMetrics
from llm_router.dashboard.query import DashboardQuery
from llm_router.dashboard.sqlite_reader import (
    DashboardQueryExecutor,
    DashboardSQLiteReader,
)
from llm_router.observability.metrics import RouterMetrics


def _client(path: Path, require_auth: bool = True) -> tuple[TestClient, DashboardQueryExecutor]:
    """Build a lightweight dashboard-only ASGI client over one fixture database."""

    app = FastAPI()
    executor = DashboardQueryExecutor()
    register_dashboard_routes(
        app, DashboardQuery(DashboardSQLiteReader(str(path))), executor,
        DashboardConfig(enabled=True, require_auth=require_auth), "client-key",
        DashboardMetrics(RouterMetrics().registry),
    )
    return TestClient(app), executor


def test_dashboard_shell_and_json_auth_contract(tmp_path: Path) -> None:
    """Shell is static while JSON requires a configured client Bearer token."""

    path = tmp_path / "dashboard.db"
    _fixture(path)
    client, executor = _client(path)
    try:
        shell = client.get("/admin")
        assert shell.status_code == 200
        assert "Content-Security-Policy" in shell.headers
        assert client.get("/admin/api/v1/overview").status_code == 401
        response = client.get("/admin/api/v1/overview", headers={"Authorization": "Bearer client-key"})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["schema_version"] == 1
        assert client.head("/admin/api/v1/overview", headers={"Authorization": "Bearer client-key"}).status_code == 200
        assert client.post("/admin/api/v1/overview", headers={"Authorization": "Bearer client-key"}).status_code == 405
    finally:
        executor.close()


def test_dashboard_rejects_unknown_filter_and_traversal(tmp_path: Path) -> None:
    """HTTP never accepts untyped query values or asset traversal paths."""

    path = tmp_path / "dashboard.db"
    _fixture(path)
    client, executor = _client(path, require_auth=False)
    try:
        assert client.get("/admin/api/v1/overview?include_sql=1").json()["error"]["code"] == "invalid_filter"
        assert client.get("/admin/assets/../app.py").status_code == 404
        assert client.get("/admin/assets/app.js").headers["content-type"].startswith("text/javascript")
    finally:
        executor.close()
