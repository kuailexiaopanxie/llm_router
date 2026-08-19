"""Immutable bounded domain values for route observability."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from llm_router.domain import AttemptEvent, Protocol


class EndpointKind(StrEnum):
    """Bound model endpoint categories."""

    MESSAGES = "messages"
    COUNT_TOKENS = "count_tokens"
    RESPONSES = "responses"


class TerminalStage(StrEnum):
    """Bound the stage at which a request became terminal."""

    AUTHENTICATION = "authentication"
    VALIDATION = "validation"
    ROUTING = "routing"
    EXECUTION_PRE_COMMIT = "execution_pre_commit"
    EXECUTION_POST_COMMIT = "execution_post_commit"
    COMPLETED = "completed"


class RequestStatus(StrEnum):
    """Bound request terminal outcomes."""

    SUCCESS = "success"
    ERROR = "error"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"


class UsageStatus(StrEnum):
    """Describe normalized Provider usage completeness."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    MISSING = "missing"
    INVALID = "invalid"
    NOT_APPLICABLE = "not_applicable"


class CostStatus(StrEnum):
    """Describe known estimated cost coverage."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    UNPRICED = "unpriced"
    USAGE_MISSING = "usage_missing"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, slots=True)
class UsageBreakdown:
    """Keep normalized Provider-reported usage and completeness."""

    status: UsageStatus
    input_uncached_tokens: int | None = None
    input_cache_read_tokens: int | None = None
    input_cache_write_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_output_tokens: int | None = None

    def __post_init__(self) -> None:
        """Validate token types and subset relationships."""

        values = (
            self.input_uncached_tokens,
            self.input_cache_read_tokens,
            self.input_cache_write_tokens,
            self.output_tokens,
            self.reasoning_output_tokens,
        )
        if any(isinstance(value, bool) or value is not None and value < 0 for value in values):
            raise ValueError("usage tokens must be non-negative integers")
        if any(value is not None and not isinstance(value, int) for value in values):
            raise ValueError("usage tokens must be non-negative integers")
        if (
            self.reasoning_output_tokens is not None
            and self.output_tokens is not None
            and self.reasoning_output_tokens > self.output_tokens
        ):
            raise ValueError("reasoning output tokens cannot exceed output tokens")

    @classmethod
    def missing(cls) -> UsageBreakdown:
        """Return a missing upstream usage observation."""

        return cls(UsageStatus.MISSING)

    @classmethod
    def not_applicable(cls) -> UsageBreakdown:
        """Return usage that is not applicable to the request."""

        return cls(UsageStatus.NOT_APPLICABLE)


@dataclass(frozen=True, slots=True)
class CostLineItem:
    """Persist one auditable priced usage category."""

    kind: str
    tokens: int
    rate_per_million: str
    amount_nanos: int


@dataclass(frozen=True, slots=True)
class CostEstimate:
    """Represent the priced subset without claiming billing truth."""

    status: CostStatus
    currency: str | None
    pricing_id: str | None
    known_amount_nanos: int | None
    line_items: tuple[CostLineItem, ...] = ()
    unknown_usage_kinds: tuple[str, ...] = ()
    unknown_invoked_attempts: int = 0


@dataclass(frozen=True, slots=True)
class TraceContext:
    """Store normalized trace identity without retaining raw headers."""

    trace_id: str
    root_span_id: str
    parent_span_id: str | None
    source: str

    def __post_init__(self) -> None:
        """Require non-zero lowercase W3C identifiers."""

        if not re.fullmatch(r"[0-9a-f]{32}", self.trace_id) or set(self.trace_id) == {"0"}:
            raise ValueError("trace_id is invalid")
        for value in (self.root_span_id, self.parent_span_id):
            if value is not None and (
                not re.fullmatch(r"[0-9a-f]{16}", value) or set(value) == {"0"}
            ):
                raise ValueError("span_id is invalid")
        if self.source not in {"generated", "remote_parent"}:
            raise ValueError("trace source is invalid")


@dataclass(frozen=True, slots=True)
class TraceSpan:
    """Store one whitelist-only local trace span."""

    trace_id: str
    span_id: str
    parent_span_id: str | None
    request_id: UUID
    name: str
    started_at: datetime
    duration_ms: float
    status: str
    attributes: Mapping[str, str | int | float | bool]

    def __post_init__(self) -> None:
        """Validate span timing and freeze its bounded attributes."""

        if self.started_at.tzinfo is None or self.duration_ms < 0:
            raise ValueError("trace span timing is invalid")
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))


@dataclass(frozen=True, slots=True)
class RoutingObservation:
    """Describe actual selected policy and deterministic routing facts."""

    requested_profile: str
    effective_profile: str | None
    started_at: datetime
    duration_ms: float
    primary_model: str | None
    target_aliases: tuple[str, ...]
    policy_version: str
    policy_hash: str
    policy_role: str
    assignment_reason: str | None
    route_reason: str | None
    auxiliary_reasons: tuple[str, ...]
    health_snapshot_revision: int
    health_filtered_count: int
    health_reason: str | None
    result: str


@dataclass(frozen=True, slots=True)
class ExecutionObservation:
    """Describe actual execution and ordered Provider attempts."""

    started_at: datetime
    duration_ms: float
    time_to_first_event_ms: float | None
    final_target: str | None
    final_provider: str | None
    attempt_count: int
    health_skipped_count: int
    committed: bool
    terminal_status: str
    attempts: tuple[AttemptEvent, ...]


@dataclass(frozen=True, slots=True)
class RouteObservation:
    """Describe one terminal model request using sanitized bounded facts."""

    request_id: UUID
    task_id: UUID | None
    trace_context: TraceContext
    received_at: datetime
    completed_at: datetime
    endpoint_kind: EndpointKind
    protocol: Protocol | None
    profile: str | None
    stream: bool
    terminal_stage: TerminalStage
    status: RequestStatus
    routing: RoutingObservation | None
    execution: ExecutionObservation | None
    usage: UsageBreakdown
    error_code: str | None
    schema_version: int = 1

    def __post_init__(self) -> None:
        """Require UTC-aware monotonic request timing."""

        if self.received_at.tzinfo is None or self.completed_at.tzinfo is None:
            raise ValueError("observation timestamps must be timezone-aware")
        if self.completed_at.astimezone(UTC) < self.received_at.astimezone(UTC):
            raise ValueError("observation completion precedes receipt")


@dataclass(frozen=True, slots=True)
class ObservationBundle:
    """Contain one terminal observation and independently enriched facts."""

    observation: RouteObservation
    cost: CostEstimate
    spans: tuple[TraceSpan, ...]
