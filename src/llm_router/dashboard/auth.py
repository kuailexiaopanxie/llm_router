"""Constant-time authentication for dashboard JSON endpoints."""

from __future__ import annotations

import secrets
from collections.abc import Mapping


def dashboard_authenticated(headers: Mapping[str, str], expected_token: str) -> bool:
    """Accept exactly one valid Bearer token without exposing its value."""

    scheme, separator, value = headers.get("authorization", "").partition(" ")
    return bool(separator and scheme.lower() == "bearer" and value and secrets.compare_digest(value, expected_token))
