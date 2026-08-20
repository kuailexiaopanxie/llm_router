"""Immutable dashboard query values and JSON read models."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class DashboardGap(StrEnum):
    """Bounded reasons for unavailable historical sections."""

    LEGACY_UNKNOWN = "legacy_unknown"
    USAGE_MISSING = "usage_missing"
    COST_UNPRICED = "cost_unpriced"
    TRACE_NOT_CAPTURED = "trace_not_captured"
    TRACE_ATTRIBUTES_INVALID = "trace_attributes_invalid"
    TRACE_INTEGRITY_GAP = "trace_integrity_gap"
    OUTCOME_NOT_OBSERVED = "outcome_not_observed"
    DECISION_NOT_CAPTURED = "decision_not_captured"


@dataclass(frozen=True, slots=True)
class DashboardFilters:
    """Bound one UTC historical observation slice."""

    start: datetime
    end: datetime
    bucket: str = "auto"
    protocols: tuple[str, ...] = ()
    endpoint_kinds: tuple[str, ...] = ("messages", "responses")
    statuses: tuple[str, ...] = ()
    terminal_stages: tuple[str, ...] = ()
    profiles: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    providers: tuple[str, ...] = ()
    policy_roles: tuple[str, ...] = ()
    route_reasons: tuple[str, ...] = ()
    fallback: bool | None = None
    task_id: UUID | None = None

    def __post_init__(self) -> None:
        """Normalize aware datetimes to UTC and reject an empty interval."""

        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("dashboard datetimes must be timezone-aware")
        start = self.start.astimezone(UTC)
        end = self.end.astimezone(UTC)
        if start >= end:
            raise ValueError("dashboard start must be before end")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)


@dataclass(frozen=True, slots=True)
class RequestCursor:
    """Locate a stable keyset page boundary."""

    before_time: datetime
    before_request: UUID

    def __post_init__(self) -> None:
        """Normalize the cursor timestamp to UTC."""

        if self.before_time.tzinfo is None:
            raise ValueError("cursor time must be timezone-aware")
        object.__setattr__(self, "before_time", self.before_time.astimezone(UTC))


@dataclass(frozen=True, slots=True)
class RequestPageQuery:
    """Describe one stable page over terminal requests."""

    filters: DashboardFilters
    cursor: RequestCursor | None = None
    limit: int = 50

    def __post_init__(self) -> None:
        """Bound the request page size."""

        if not 1 <= self.limit <= 100:
            raise ValueError("request page limit must be between 1 and 100")


@dataclass(frozen=True, slots=True)
class DashboardTargetHealth:
    """Expose one sanitized current target health fact."""

    alias: str
    provider: str
    state: str
    eligible: bool
    retry_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DashboardRuntimeSnapshot:
    """Expose current sanitized process facts."""

    observed_at: datetime
    ready: bool
    started_at: datetime
    router_version: str
    capture_enabled: bool
    trace_enabled: bool
    local_trace_store: bool
    observation_queue_depth: int
    observation_queue_capacity: int
    observation_dropped_since_start: int
    sqlite_failures_since_start: int
    canary_enabled: bool
    canary_active: bool
    canary_reason: str | None
    health_revision: int
    targets: tuple[DashboardTargetHealth, ...]
    status: str = "available"


@dataclass(frozen=True, slots=True)
class OverviewSnapshot:
    """Hold one immutable overview response payload."""

    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RequestPage:
    """Hold one immutable keyset page payload."""

    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RequestDetail:
    """Hold one immutable request detail payload."""

    payload: dict[str, Any]


def json_value(value: Any) -> Any:
    """Convert dashboard values to JSON-safe primitives without float nanos."""

    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, StrEnum):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return json_value(asdict(value))
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(item) for item in value]
    return value

