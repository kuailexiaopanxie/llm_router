"""Read-only SQLite adapter tests."""

import sqlite3
from pathlib import Path

import pytest

from llm_router.dashboard.sqlite_reader import (
    DashboardQueryError,
    DashboardSQLiteReader,
)


def _database(path: Path) -> None:
    """Create a minimal required route schema fixture."""

    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE route_requests (request_id TEXT PRIMARY KEY, received_at TEXT, protocol TEXT, profile TEXT, stream INTEGER, primary_model TEXT, final_model TEXT, route_reason TEXT, policy_version TEXT, status TEXT, attempt_count INTEGER, total_latency_ms REAL, input_tokens INTEGER, output_tokens INTEGER, error_code TEXT)")


def test_reader_is_read_only_and_does_not_create_missing_database(tmp_path: Path) -> None:
    """Read-only connections reject writes and missing paths remain absent."""

    missing = tmp_path / "missing.db"
    with pytest.raises(DashboardQueryError) as error, DashboardSQLiteReader(str(missing)).transaction():
        pass
    assert error.value.code == "observation_unavailable"
    assert not missing.exists()

    path = tmp_path / "router.db"
    _database(path)
    with DashboardSQLiteReader(str(path)).transaction() as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("CREATE TABLE forbidden (value TEXT)")
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
