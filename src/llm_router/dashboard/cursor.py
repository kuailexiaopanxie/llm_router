"""Versioned dashboard keyset cursor codec."""

from __future__ import annotations

import base64
import json
from datetime import datetime
from uuid import UUID

from llm_router.dashboard.models import RequestCursor


def encode_cursor(cursor: RequestCursor) -> str:
    """Encode one cursor as compact unpadded base64url JSON."""

    payload = {
        "v": 1,
        "before_time": cursor.before_time.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "before_request": str(cursor.before_request),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(value: str) -> RequestCursor:
    """Decode and strictly validate one versioned keyset cursor."""

    if not value or len(value) > 512 or any(character.isspace() for character in value):
        raise ValueError("invalid cursor")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        payload = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) != {"v", "before_time", "before_request"}:
            raise ValueError
        if payload["v"] != 1:
            raise ValueError
        timestamp = datetime.fromisoformat(str(payload["before_time"]).replace("Z", "+00:00"))
        return RequestCursor(timestamp, UUID(str(payload["before_request"])))
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError("invalid cursor") from exc

