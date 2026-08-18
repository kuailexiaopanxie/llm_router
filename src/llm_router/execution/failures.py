"""Pure bounded classification for upstream and transport failures."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from llm_router.errors import RouterError, cancelled_error, timeout_error
from llm_router.health.models import FailureClass

_PROVIDER_TRANSIENT_STATUS = {500, 502, 503, 504, 529}


@dataclass(frozen=True, slots=True)
class FailureDecision:
    """Bounded failure classification used by execution and health."""

    failure_class: FailureClass
    router_error: RouterError
    retry_after_seconds: float | None = None


def classify_http_status(status_code: int) -> FailureClass:
    """Map an upstream HTTP status to its default failure class."""

    if 200 <= status_code < 300:
        return FailureClass.SUCCESS
    if status_code == 401:
        return FailureClass.PROVIDER_PERMANENT
    if status_code == 404:
        return FailureClass.TARGET_PERMANENT
    if status_code == 429:
        return FailureClass.TARGET_TRANSIENT
    if status_code in _PROVIDER_TRANSIENT_STATUS:
        return FailureClass.PROVIDER_TRANSIENT
    return FailureClass.REQUEST_REJECTED


def parse_retry_after(
    value: str | None,
    now: datetime,
    max_seconds: float,
) -> float | None:
    """Parse Retry-After delta-seconds or HTTP-date and cap its duration."""

    if value is None:
        return None
    normalized = value.strip()
    if normalized.isdigit():
        return min(max_seconds, float(normalized))
    try:
        retry_at = parsedate_to_datetime(normalized)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    seconds = max(0.0, (retry_at - now).total_seconds())
    return min(max_seconds, seconds)


def router_error_for_failure(failure_class: FailureClass) -> RouterError:
    """Map one sanitized failure class to a safe client-facing error."""

    if failure_class is FailureClass.CLIENT_CANCELLED:
        return cancelled_error()
    if failure_class is FailureClass.PROVIDER_TRANSIENT:
        return RouterError(
            "router_upstream_retryable",
            503,
            "The upstream request failed.",
            fallback_allowed=True,
        )
    if failure_class is FailureClass.TARGET_TRANSIENT:
        return RouterError(
            "router_upstream_retryable",
            503,
            "The upstream target is temporarily unavailable.",
            fallback_allowed=True,
        )
    if failure_class is FailureClass.POST_COMMIT_STREAM_FAILURE:
        return RouterError(
            "router_upstream_read_failed",
            502,
            "The upstream stream failed.",
        )
    if failure_class is FailureClass.SUCCESS:
        return RouterError("router_internal_error", 500, "The request could not be processed.")
    return RouterError(
        "router_upstream_rejected",
        502,
        "The upstream rejected the request.",
    )


def decision_for_http(
    status_code: int,
    retry_after: str | None,
    now: datetime,
    max_retry_after_seconds: float,
) -> FailureDecision:
    """Classify an HTTP response and its bounded Retry-After metadata."""

    failure_class = classify_http_status(status_code)
    retry_after_seconds = parse_retry_after(
        retry_after,
        now,
        max_retry_after_seconds,
    )
    router_error = router_error_for_failure(failure_class)
    router_error.retry_after = retry_after_seconds
    return FailureDecision(
        failure_class=failure_class,
        router_error=router_error,
        retry_after_seconds=retry_after_seconds,
    )


def timeout_decision(total_deadline_exceeded: bool = False) -> FailureDecision:
    """Classify execution timeout without inspecting exception messages."""

    if total_deadline_exceeded:
        return FailureDecision(FailureClass.REQUEST_REJECTED, timeout_error())
    failure_class = FailureClass.PROVIDER_TRANSIENT
    return FailureDecision(failure_class, router_error_for_failure(failure_class))


def decision_for_router_error(error: RouterError) -> FailureDecision:
    """Classify known pre-commit router errors without inspecting messages."""

    transient_codes = {
        "router_upstream_empty_stream",
        "router_upstream_header_timeout",
        "router_upstream_invalid_stream",
        "router_upstream_read_failed",
    }
    failure_class = (
        FailureClass.PROVIDER_TRANSIENT
        if error.code in transient_codes
        else FailureClass.REQUEST_REJECTED
    )
    return FailureDecision(failure_class, error)
