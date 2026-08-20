"""Strict HTTP query parsing for bounded dashboard reads."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID

from llm_router.dashboard.config import DashboardConfig
from llm_router.dashboard.cursor import decode_cursor
from llm_router.dashboard.models import DashboardFilters, RequestPageQuery

_SAFE_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_MULTI = {
    "protocol": "protocols", "endpoint_kind": "endpoint_kinds", "status": "statuses",
    "terminal_stage": "terminal_stages", "profile": "profiles", "model": "models",
    "provider": "providers", "policy_role": "policy_roles", "route_reason": "route_reasons",
}
_BUCKET_SECONDS = {"5m": 300, "1h": 3600, "6h": 21600, "1d": 86400, "7d": 604800}
_ENUM_VALUES = {
    "protocol": {"anthropic_messages", "openai_responses", "unknown"},
    "endpoint_kind": {"messages", "responses", "count_tokens"},
    "status": {"success", "error", "cancelled", "abandoned"},
    "terminal_stage": {"authentication", "validation", "routing", "execution_pre_commit", "execution_post_commit"},
    "policy_role": {"control", "canary", "current", "candidate", "legacy_unknown"},
}


def _values(query: Mapping[str, object], key: str) -> tuple[str, ...]:
    """Read repeated query values from Starlette or plain mappings."""

    getter = getattr(query, "getlist", None)
    raw = getter(key) if callable(getter) else query.get(key, ())
    values = (raw,) if isinstance(raw, str) else tuple(raw) if isinstance(raw, Sequence) else ()
    if len(values) > 20 or any(not _SAFE_VALUE.fullmatch(str(value)) for value in values):
        raise ValueError("invalid filter")
    return tuple(str(value) for value in values)


def _instant(value: object, default: datetime) -> datetime:
    """Parse one RFC3339 instant and normalize it to UTC."""

    if value is None:
        return default
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid filter") from exc
    if parsed.tzinfo is None:
        raise ValueError("invalid filter")
    return parsed.astimezone(UTC)


def _parse_filters(
    query: Mapping[str, object], config: DashboardConfig, now: datetime | None,
    extra_parameters: frozenset[str],
) -> DashboardFilters:
    """Parse filters while allowing caller-owned pagination parameters."""

    allowed = {"from", "to", "bucket", "fallback", "task_id", *_MULTI}
    if set(query) - allowed - extra_parameters:
        raise ValueError("invalid filter")
    current = (now or datetime.now(UTC)).astimezone(UTC)
    end = _instant(query.get("to"), current)
    start = _instant(query.get("from"), end - timedelta(hours=config.default_range_hours))
    if end > current + timedelta(minutes=5) or end - start > timedelta(days=config.max_range_days):
        raise ValueError("invalid filter")
    bucket = str(query.get("bucket", "auto"))
    if bucket not in {"auto", *_BUCKET_SECONDS}:
        raise ValueError("invalid filter")
    explicit = _BUCKET_SECONDS.get(bucket)
    if explicit and (end - start).total_seconds() / explicit > 500:
        raise ValueError("invalid filter")
    fallback_value = query.get("fallback")
    if fallback_value not in (None, "true", "false"):
        raise ValueError("invalid filter")
    task_value = query.get("task_id")
    try:
        task = UUID(str(task_value)) if task_value is not None else None
    except ValueError as exc:
        raise ValueError("invalid filter") from exc
    fields = {attribute: _values(query, name) for name, attribute in _MULTI.items()}
    for name, accepted in _ENUM_VALUES.items():
        if any(value not in accepted for value in fields[_MULTI[name]]):
            raise ValueError("invalid filter")
    if "endpoint_kind" not in query:
        fields["endpoint_kinds"] = ("messages", "responses")
    return DashboardFilters(
        start=start, end=end, bucket=bucket,
        fallback=None if fallback_value is None else fallback_value == "true", task_id=task, **fields,
    )


def parse_filters(
    query: Mapping[str, object], config: DashboardConfig, now: datetime | None = None
) -> DashboardFilters:
    """Validate HTTP query values before any SQL is constructed."""

    return _parse_filters(query, config, now, frozenset())


def parse_request_page(
    query: Mapping[str, object], config: DashboardConfig, now: datetime | None = None
) -> RequestPageQuery:
    """Parse common filters plus bounded keyset pagination values."""

    if set(query) - {"cursor", "limit", "from", "to", "bucket", "fallback", "task_id", *_MULTI}:
        raise ValueError("invalid filter")
    try:
        limit = int(str(query.get("limit", 50)))
        cursor = decode_cursor(str(query["cursor"])) if query.get("cursor") is not None else None
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid filter") from exc
    return RequestPageQuery(
        _parse_filters(query, config, now, frozenset({"cursor", "limit"})), cursor, limit
    )


def validate_query_length(raw_query: bytes) -> None:
    """Reject query strings above the fixed 8 KiB input bound."""

    if len(raw_query) > 8192:
        raise ValueError("invalid filter")
