"""Dashboard filter contract tests."""

from datetime import UTC, datetime, timedelta

import pytest

from llm_router.dashboard.config import DashboardConfig
from llm_router.dashboard.filters import parse_filters, parse_request_page


def test_filter_defaults_to_utc_24_hours() -> None:
    """Default range is normalized to UTC and endpoint kinds exclude count tokens."""

    now = datetime(2026, 8, 19, 12, tzinfo=UTC)
    filters = parse_filters({}, DashboardConfig(), now)
    assert filters.end == now
    assert filters.end - filters.start == timedelta(hours=24)
    assert filters.endpoint_kinds == ("messages", "responses")


def test_repeated_filters_and_page_bounds() -> None:
    """Repeated safe values survive typed parsing and page limits stay bounded."""

    query = {"status": ["error", "cancelled"], "limit": "2"}
    page = parse_request_page(query, DashboardConfig())
    assert page.filters.statuses == ("error", "cancelled")
    assert page.limit == 2


@pytest.mark.parametrize("query", [{"unknown": "x"}, {"status": "error OR 1=1"}, {"fallback": "yes"}, {"bucket": "5m", "from": "2026-08-01T00:00:00Z", "to": "2026-08-03T00:00:00Z"}])
def test_invalid_filters_are_rejected(query: dict[str, str]) -> None:
    """Reject unknown, unsafe, invalid enum, and over-wide bucket inputs."""

    with pytest.raises(ValueError):
        parse_filters(query, DashboardConfig(), datetime(2026, 8, 19, tzinfo=UTC))
