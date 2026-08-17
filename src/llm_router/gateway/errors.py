"""Stable protocol-neutral router error taxonomy."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RouterError(Exception):
    """An expected router or upstream failure safe to expose to a client."""

    code: str
    http_status: int
    safe_message: str
    fallback_allowed: bool = False
    retry_after: float | None = None

    def __str__(self) -> str:
        """Return the sanitized English error message."""

        return self.safe_message


def invalid_request(message: str = "The request is invalid.") -> RouterError:
    """Create a normalized 400 request error."""

    return RouterError("router_invalid_request", 400, message)


def no_capable_model() -> RouterError:
    """Create the deterministic capability mismatch error."""

    return RouterError(
        "router_no_capable_model",
        422,
        "The requested route has no capable model.",
    )


def unknown_model() -> RouterError:
    """Create the unknown profile/model error."""

    return RouterError("router_unknown_model", 400, "The requested model or profile is not configured.")


def unauthorized() -> RouterError:
    """Create a local authentication error."""

    return RouterError("router_unauthorized", 401, "The local API key is invalid.")


def upstream_exhausted() -> RouterError:
    """Create the error returned after all planned attempts fail."""

    return RouterError("router_upstream_exhausted", 503, "All planned upstream attempts were exhausted.")


def timeout_error() -> RouterError:
    """Create a total execution deadline error."""

    return RouterError("router_timeout", 504, "The upstream execution deadline was exceeded.")


def response_header_timeout() -> RouterError:
    """Create a retryable upstream response-header timeout."""

    return RouterError(
        "router_upstream_header_timeout",
        503,
        "The upstream response header timed out.",
        fallback_allowed=True,
    )


def cancelled_error() -> RouterError:
    """Create a client cancellation error."""

    return RouterError("router_cancelled", 499, "The client cancelled the request.")


def upstream_rejected() -> RouterError:
    """Create a non-retryable upstream rejection error."""

    return RouterError("router_upstream_rejected", 502, "The upstream rejected the request.")


def not_ready() -> RouterError:
    """Create the readiness failure error."""

    return RouterError("router_not_ready", 503, "The router is not ready.")
