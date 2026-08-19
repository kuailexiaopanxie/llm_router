"""Shared immutable domain objects for routing and execution."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from llm_router.observability.models import UsageBreakdown


class Capability(StrEnum):
    """Model capabilities that can be required by a request."""

    STREAMING = "streaming"
    TOOLS = "tools"
    THINKING = "thinking"
    VISION = "vision"
    PROMPT_CACHE = "prompt_cache"
    REASONING = "reasoning"
    STRUCTURED_OUTPUT = "structured_output"
    RESPONSE_STATE = "response_state"
    PROVIDER_MANAGED_TOOLS = "provider_managed_tools"


class Protocol(StrEnum):
    """Inbound and upstream protocol families supported by the router."""

    ANTHROPIC_MESSAGES = "anthropic_messages"
    OPENAI_RESPONSES = "openai_responses"


class Tier(StrEnum):
    """Ordered model quality tiers used by deterministic policy."""

    FAST = "fast"
    BALANCED = "balanced"
    DEEP = "deep"


class OutcomeSignal(StrEnum):
    """Conservative outcome extracted from explicit tool results."""

    SUCCESS = "success"
    FAILURE = "failure"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ProtocolEnvelope:
    """Preserve the inbound protocol body and approved headers for one request."""

    request_id: str
    protocol: Protocol
    raw_body: Mapping[str, Any]
    safe_headers: Mapping[str, str]
    stream: bool
    received_at: datetime
    endpoint: str = "/v1/messages"
    traceparent: str | None = None


@dataclass(frozen=True, slots=True)
class TaskSignals:
    """Bounded routing signals derived without retaining source text."""

    complex_planning: bool = False
    debugging: bool = False
    review: bool = False
    multi_file_refactor: bool = False


@dataclass(frozen=True, slots=True)
class RoutingRequest:
    """Sanitized input consumed by the pure routing kernel."""

    requested_profile: str
    required_capabilities: frozenset[Capability]
    estimated_input_tokens: int
    message_count: int
    tool_rounds: int
    system_size_bucket: str
    task_signals: TaskSignals
    outcome_signal: OutcomeSignal
    stream: bool
    count_only: bool = False
    protocol: Protocol = Protocol.ANTHROPIC_MESSAGES
    response_state_requested: bool = False
    provider_managed_tools_requested: bool = False


@dataclass(frozen=True, slots=True)
class ModelTarget:
    """Resolved provider target safe for use by the execution engine."""

    alias: str
    provider: str
    upstream_model: str
    tier: Tier
    capabilities: frozenset[Capability]
    max_input_tokens: int
    input_price_per_million: float | None
    output_price_per_million: float | None
    protocol: Protocol = Protocol.ANTHROPIC_MESSAGES
    state_scope: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutionTimeouts:
    """Timeout budget embedded into an immutable execution plan."""

    connect_seconds: float
    response_header_seconds: float
    non_stream_deadline_seconds: float
    stream_idle_seconds: float
    stream_max_seconds: float


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """Immutable model selection contract between routing and execution."""

    primary: ModelTarget
    fallbacks: tuple[ModelTarget, ...]
    attempt_limit: int
    timeouts: ExecutionTimeouts
    route_reason: str
    auxiliary_reasons: tuple[str, ...]
    profile: str
    policy_version: str
    health_snapshot_revision: int = 0
    health_filtered_count: int = 0
    health_reason: str | None = None

    @property
    def targets(self) -> tuple[ModelTarget, ...]:
        """Return the bounded, ordered attempt sequence."""

        return (self.primary, *self.fallbacks)[: self.attempt_limit]


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    """Provider invocation containing only protocol and resolved target data."""

    envelope: ProtocolEnvelope
    target: ModelTarget
    connect_timeout: float
    response_header_timeout: float


@dataclass(slots=True)
class ProviderExchange:
    """Open upstream response whose lifetime is owned by the execution engine."""

    status_code: int
    headers: Mapping[str, str]
    body: AsyncIterator[bytes]
    close: Any


@dataclass(frozen=True, slots=True)
class AttemptEvent:
    """Sanitized observation for a single provider attempt."""

    request_id: str
    sequence: int
    provider: str
    model: str
    started_at: datetime
    duration_ms: float
    status: str
    http_status: int | None = None
    error_code: str | None = None
    upstream_invoked: bool = True


@dataclass(frozen=True, slots=True)
class ExecutionStats:
    """Final execution measurements shared with observability."""

    status: str
    total_latency_ms: float
    time_to_first_event_ms: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    error_code: str | None = None
    usage: UsageBreakdown | None = None


@dataclass(frozen=True, slots=True)
class ExecutionFailureSnapshot:
    """Preserve bounded failed execution facts on a RouterError."""

    started_at: datetime
    duration_ms: float
    attempts: tuple[AttemptEvent, ...]
    upstream_attempt_count: int
    health_skipped_count: int
    committed: bool = False


@dataclass(slots=True)
class ProxyResponse:
    """Prepared downstream response returned before an SSE body is consumed."""

    status_code: int
    headers: dict[str, str]
    body: bytes | AsyncIterator[bytes]
    media_type: str
    final_target: ModelTarget
    attempt_count: int
    completion: Any
    attempts: tuple[AttemptEvent, ...] = field(default_factory=tuple)
    health_skipped_count: int = 0


@dataclass(frozen=True, slots=True)
class FeatureSummary:
    """Persistable bounded request features."""

    required_capabilities: tuple[str, ...]
    input_size_bucket: str
    message_count_bucket: str
    tool_rounds_bucket: str
    outcome_signal: str
    task_signal_count: int
