"""Dashboard Requests page and detail contract tests."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from test_v08_dashboard_overview import _fixture

from llm_router.dashboard.models import DashboardFilters, RequestPageQuery
from llm_router.dashboard.query import DashboardQuery
from llm_router.dashboard.sqlite_reader import DashboardSQLiteReader


def test_keyset_requests_have_no_duplicate_rows(tmp_path: Path) -> None:
    """Keyset pagination advances over the deterministic received/id ordering."""

    path = tmp_path / "dashboard.db"
    first_id, _ = _fixture(path)
    filters = DashboardFilters(datetime(2026, 8, 19, 9, tzinfo=UTC), datetime(2026, 8, 19, 12, tzinfo=UTC))
    query = DashboardQuery(DashboardSQLiteReader(str(path)))
    first = query.requests(RequestPageQuery(filters, limit=1)).payload
    assert first["has_more"] is True
    assert first["items"][0]["request_id"] != first_id
    from llm_router.dashboard.cursor import decode_cursor

    second = query.requests(RequestPageQuery(filters, decode_cursor(first["next_cursor"]), 1)).payload
    assert second["items"][0]["request_id"] != first["items"][0]["request_id"]


def test_detail_keeps_attempts_cost_and_section_gaps(tmp_path: Path) -> None:
    """Detail reads child facts in order and exposes absent trace as a gap."""

    path = tmp_path / "dashboard.db"
    _, fallback_id = _fixture(path)
    detail = DashboardQuery(DashboardSQLiteReader(str(path))).request_detail(UUID(fallback_id))
    assert detail is not None
    payload = detail.payload
    assert [item["sequence"] for item in payload["execution"]["attempts"]] == [1, 2]
    assert payload["routing"]["final_provider"] == "anthropic"
    assert payload["cost"]["known_amount_nanos"] == "2000000000"
    assert payload["trace"]["gap"] == "trace_not_captured"


def test_missing_detail_returns_none(tmp_path: Path) -> None:
    """An absent request stays distinguishable from missing child facts."""

    path = tmp_path / "dashboard.db"
    _fixture(path)
    assert DashboardQuery(DashboardSQLiteReader(str(path))).request_detail(UUID(int=99)) is None
