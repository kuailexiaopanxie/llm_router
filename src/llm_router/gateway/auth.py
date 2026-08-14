"""Local authentication and safe request header handling."""

from __future__ import annotations

import re
import secrets
import uuid
from collections.abc import Mapping

from llm_router.gateway.errors import unauthorized


_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
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
    """Accept a bounded client request ID or generate a UUID4."""

    candidate = headers.get("x-request-id")
    if candidate and _REQUEST_ID.fullmatch(candidate):
        return candidate
    return uuid.uuid4().hex


def safe_headers(headers: Mapping[str, str], extension_headers: tuple[str, ...] = ()) -> dict[str, str]:
    """Return only protocol headers approved for upstream forwarding."""

    allowed = _BASE_ALLOWED | {header.lower() for header in extension_headers}
    return {key.lower(): value for key, value in headers.items() if key.lower() in allowed}
