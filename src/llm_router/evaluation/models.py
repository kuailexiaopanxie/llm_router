"""Typed, bounded models for Outcome capture and decision replay."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from llm_router.domain import ExecutionPlan, RoutingRequest
from llm_router.errors import RouterError
from llm_router.health.models import AvailabilitySnapshot
from llm_router.routing.context import SessionSnapshot


class OutcomeVerdict(StrEnum):
    """Bounded client-reported task result."""

    SUCCESS = "success"
    FAILURE = "failure"
    PARTIAL = "partial"


class OutcomeEvidence(StrEnum):
    """Bounded evidence kind attached to one result."""

    PATCH_APPLY = "patch_apply"
    COMPILE = "compile"
    LINT = "lint"
    TEST = "test"
    TOOL = "tool"
    TASK = "task"


class OutcomeSource(StrEnum):
    """Bounded producer category."""

    CLIENT = "client"
    IDE = "ide"
    CI = "ci"
    INTEGRATION = "integration"


class Correlation(StrEnum):
    """Current relationship between an Outcome and router facts."""

    MATCHED = "matched"
    PENDING = "pending"
    TASK_MISMATCH = "task_mismatch"
    UNASSIGNED = "unassigned"


@dataclass(frozen=True, slots=True)
class OutcomeEvent:
    """Store one bounded observation about a routed request."""

    event_id: UUID
    request_id: UUID
    task_id: UUID | None
    verdict: OutcomeVerdict
    evidence: OutcomeEvidence
    source: OutcomeSource
    observed_at: datetime | None
    received_at: datetime


@dataclass(frozen=True, slots=True)
class OutcomeReceipt:
    """Return synchronous persistence and correlation status."""

    event_id: UUID
    status: str
    correlation: Correlation
    actual_target: str | None = None


@dataclass(frozen=True, slots=True)
class RouterErrorSnapshot:
    """Persist the safe, bounded shape of an expected routing error."""

    code: str
    http_status: int
    fallback_allowed: bool
    retry_after: float | None = None
    health_snapshot_revision: int = 0
    health_filtered_count: int = 0
    health_reason: str | None = None

    @classmethod
    def from_error(cls, error: RouterError) -> RouterErrorSnapshot:
        """Convert a RouterError without retaining its message or payload."""

        return cls(
            code=error.code,
            http_status=error.http_status,
            fallback_allowed=error.fallback_allowed,
            retry_after=error.retry_after,
            health_snapshot_revision=error.health_snapshot_revision,
            health_filtered_count=error.health_filtered_count,
            health_reason=error.health_reason,
        )


@dataclass(frozen=True, slots=True)
class RouteDecisionInput:
    """Minimum sanitized input and actual result required for replay."""

    request_id: UUID
    task_id: UUID | None
    recorded_at: datetime
    router_version: str
    routing_algorithm_version: str
    routing_policy_hash: str
    request: RoutingRequest
    session: SessionSnapshot | None
    availability: AvailabilitySnapshot
    actual_plan: ExecutionPlan | None = None
    actual_error: RouterErrorSnapshot | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        """Require exactly one normalized actual result."""

        if (self.actual_plan is None) == (self.actual_error is None):
            raise ValueError("a decision input must contain exactly one actual result")


@dataclass(frozen=True, slots=True)
class RoutingPolicySnapshot:
    """Persist a canonical, secret-free policy for historical replay."""

    routing_policy_hash: str
    policy_version: str
    routing_algorithm_version: str
    policy_json: str
    created_at: datetime
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class ReplayCase:
    """Pair one historical decision with its policy and optional facts."""

    decision: RouteDecisionInput
    historical_policy: RoutingPolicySnapshot | None
    outcomes: tuple[OutcomeEvent, ...] = ()


class ReplayStatus(StrEnum):
    """Whether a candidate could be evaluated."""

    REPLAYED = "replayed"
    NON_REPLAYABLE = "non_replayable"


class ReplayMode(StrEnum):
    """Select historical or deterministic healthy availability."""

    HISTORICAL = "historical"
    ALL_HEALTHY = "all-healthy"


class ReplayChange(StrEnum):
    """Execution-shape difference between actual and candidate decisions."""

    UNCHANGED = "unchanged"
    PRIMARY_CHANGED = "primary_changed"
    CHAIN_CHANGED = "chain_changed"
    ERROR_CHANGED = "error_changed"
    PLAN_TO_ERROR = "plan_to_error"
    ERROR_TO_PLAN = "error_to_plan"


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """Safe comparison result with no hypothetical quality claims."""

    request_id: UUID
    status: ReplayStatus
    historical_policy_hash: str
    candidate_policy_hash: str
    actual_plan: ExecutionPlan | None
    actual_error: RouterErrorSnapshot | None
    candidate_plan: ExecutionPlan | None = None
    candidate_error: RouterErrorSnapshot | None = None
    change: ReplayChange | None = None
    reason: str | None = None
    mode: str = "historical"
