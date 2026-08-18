"""Pure failure-classification and Retry-After tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest

from llm_router.execution.failures import (
    classify_http_status,
    decision_for_http,
    parse_retry_after,
)
from llm_router.health.models import FailureClass


NOW = datetime(2026, 8, 18, tzinfo=UTC)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (200, FailureClass.SUCCESS),
        (400, FailureClass.REQUEST_REJECTED),
        (401, FailureClass.PROVIDER_PERMANENT),
        (404, FailureClass.TARGET_PERMANENT),
        (422, FailureClass.REQUEST_REJECTED),
        (429, FailureClass.TARGET_TRANSIENT),
        (500, FailureClass.PROVIDER_TRANSIENT),
        (529, FailureClass.PROVIDER_TRANSIENT),
    ],
)
def test_http_status_classification(status: int, expected: FailureClass) -> None:
    """Keep HTTP failure mapping deterministic and message-independent."""

    assert classify_http_status(status) is expected


def test_retry_after_delta_and_http_date_are_capped() -> None:
    """Parse both allowed Retry-After forms without exceeding the configured cap."""

    assert parse_retry_after("120", NOW, 60) == 60
    future = format_datetime(NOW + timedelta(seconds=45), usegmt=True)
    assert parse_retry_after(future, NOW, 60) == 45


@pytest.mark.parametrize("value", [None, "", "-1", "not-a-date"])
def test_invalid_retry_after_is_ignored(value: str | None) -> None:
    """Ignore invalid or negative Retry-After values."""

    assert parse_retry_after(value, NOW, 60) is None


def test_only_transient_http_failures_allow_fallback() -> None:
    """Keep permanent and request failures from triggering fallback."""

    transient = decision_for_http(503, None, NOW, 300)
    permanent = decision_for_http(401, None, NOW, 300)

    assert transient.router_error.fallback_allowed is True
    assert permanent.router_error.fallback_allowed is False
