"""Local authentication and safe request header handling."""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Mapping

from llm_router.errors import invalid_request, unauthorized

_BASE_ALLOWED = {"anthropic-version", "anthropic-beta", "content-type", "accept"}


def authenticate(headers: Mapping[str, str], expected_token: str) -> None:
    """Validate x-api-key and Bearer credentials using constant-time comparison."""

    api_key = headers.get("x-api-key")
    authorization = headers.get("authorization")
    bearer = None
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value:
            bearer = value
    if api_key is not None and bearer is not None and not secrets.compare_digest(api_key, bearer):
        raise unauthorized()
    provided = api_key if api_key is not None else bearer
    if provided is None or not secrets.compare_digest(provided, expected_token):
        raise unauthorized()


def request_id(headers: Mapping[str, str]) -> str:
    """Generate a Router-owned UUID4 independent of client request IDs."""

    return str(uuid.uuid4())


def task_id(headers: Mapping[str, str]) -> str | None:
    """Validate an optional opaque task UUID without forwarding it upstream."""

    candidate = headers.get("x-llm-router-task-id")
    if candidate is None:
        return None
    try:
        return str(uuid.UUID(candidate))
    except (ValueError, AttributeError) as exc:
        raise invalid_request("The x-llm-router-task-id header must be a UUID.") from exc


def safe_headers(headers: Mapping[str, str], extension_headers: tuple[str, ...] = ()) -> dict[str, str]:
    """Return only protocol headers approved for upstream forwarding."""

    allowed = _BASE_ALLOWED | {header.lower() for header in extension_headers}
    return {key.lower(): value for key, value in headers.items() if key.lower() in allowed}
