"""Dashboard Overview aggregation tests."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from llm_router.dashboard.models import DashboardFilters
from llm_router.dashboard.query import DashboardQuery
from llm_router.dashboard.sqlite_reader import DashboardSQLiteReader


def _fixture(path: Path) -> tuple[str, str]:
    """Create deterministic success, fallback, and mixed-cost observations."""

    with sqlite3.connect(path) as db:
        db.executescript("""
        CREATE TABLE route_requests (request_id TEXT PRIMARY KEY, task_id TEXT, received_at TEXT, completed_at TEXT, protocol TEXT, profile TEXT, stream INTEGER, primary_model TEXT, final_model TEXT, route_reason TEXT, policy_version TEXT, status TEXT, attempt_count INTEGER, total_latency_ms REAL, input_tokens INTEGER, output_tokens INTEGER, error_code TEXT, endpoint_kind TEXT, terminal_stage TEXT, policy_role TEXT, known_cost_nanos INTEGER, cost_currency TEXT, cost_status TEXT, unknown_cost_attempts INTEGER, trace_id TEXT, trace_captured INTEGER, usage_status TEXT);
        CREATE TABLE route_attempts (request_id TEXT, sequence INTEGER, provider TEXT, model TEXT, started_at TEXT, duration_ms REAL, status TEXT, http_status INTEGER, error_code TEXT, upstream_invoked INTEGER);
        """)
        now = datetime(2026, 8, 19, 10, tzinfo=UTC)
        rows = [
            ("00000000-0000-0000-0000-000000000001", None, now.isoformat(), now.isoformat(), "anthropic_messages", "code/auto", 0, "fast", "fast", "tier_fast", "v2", "success", 1, 100, 10, 5, None, "messages", "completed", "control", 1000000000, "USD", "complete", 0, None, 0, "complete"),
            ("00000000-0000-0000-0000-000000000002", None, (now + timedelta(minutes=1)).isoformat(), (now + timedelta(minutes=1, seconds=2)).isoformat(), "anthropic_messages", "code/auto", 0, "fast", "deep", "fallback", "v2", "success", 2, 2000, 20, 8, None, "messages", "completed", "canary", 2000000000, "EUR", "partial", 1, None, 0, "partial"),
            ("00000000-0000-0000-0000-000000000003", None, (now + timedelta(minutes=2)).isoformat(), (now + timedelta(minutes=2, seconds=3)).isoformat(), "anthropic_messages", "code/auto", 0, "deep", "deep", "error", "v2", "error", 1, 3000, None, None, "timeout", "messages", "execution_pre_commit", "control", None, None, "usage_missing", 0, None, 0, "missing"),
        ]
        db.executemany("INSERT INTO route_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        db.executemany("INSERT INTO route_attempts VALUES (?,?,?,?,?,?,?,?,?,?)", [(rows[0][0],1,"anthropic","fast",now.isoformat(),100,"success",200,None,1),(rows[1][0],1,"anthropic","fast",now.isoformat(),100,"error",500,"timeout",1),(rows[1][0],2,"anthropic","deep",now.isoformat(),100,"success",200,None,1),(rows[2][0],1,"anthropic","deep",now.isoformat(),100,"error",500,"timeout",1)])
    return rows[0][0], rows[1][0]


def test_overview_preserves_fallback_and_currency_semantics(tmp_path: Path) -> None:
    """Overview counts error denominator, actual fallback, and separate currencies."""

    path = tmp_path / "dashboard.db"
    _fixture(path)
    filters = DashboardFilters(datetime(2026, 8, 19, 9, tzinfo=UTC), datetime(2026, 8, 19, 12, tzinfo=UTC))
    payload = DashboardQuery(DashboardSQLiteReader(str(path))).overview(filters).payload
    assert payload["summary"]["requests"] == 3
    assert payload["summary"]["success"]["denominator"] == 3
    assert payload["summary"]["fallback"]["numerator"] == 1
    assert {item["currency"] for item in payload["summary"]["cost"]["known_amounts"]} == {"EUR", "USD"}
    assert payload["summary"]["cost"]["known_amounts"][0]["known_amount_nanos"] in {"1000000000", "2000000000"}
