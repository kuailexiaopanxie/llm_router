"""Immutable domain objects for bounded provider health management."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class HealthState(StrEnum):
    """Operational eligibility state of one failure domain."""

    HEALTHY = "healthy"
    COOLDOWN = "cooldown"
    HALF_OPEN = "half_open"
    BLOCKED = "blocked"


class FailureClass(StrEnum):
    """Sanitized outcome classes accepted by health management."""

    SUCCESS = "success"
    PROVIDER_TRANSIENT = "provider_transient"
    TARGET_TRANSIENT = "target_transient"
    PROVIDER_PERMANENT = "provider_permanent"
    TARGET_PERMANENT = "target_permanent"
    REQUEST_REJECTED = "request_rejected"
    CLIENT_CANCELLED = "client_cancelled"
    POST_COMMIT_STREAM_FAILURE = "post_commit_stream_failure"


class AvailabilityReason(StrEnum):
    """Bounded reason exposed to deterministic routing."""

    HEALTHY = "healthy"
    PROVIDER_COOLDOWN = "provider_cooldown"
    TARGET_COOLDOWN = "target_cooldown"
    PROVIDER_BLOCKED = "provider_blocked"
    TARGET_BLOCKED = "target_blocked"
    PROBE_ELIGIBLE = "probe_eligible"


class FailureScope(StrEnum):
    """Configured failure-domain scope of a recovery probe."""

    PROVIDER = "provider"
    TARGET = "target"


@dataclass(frozen=True, slots=True)
class TargetAvailability:
    """Immutable availability facts for one configured Model Target."""

    eligible: bool
    state: HealthState
    reason: AvailabilityReason
    retry_at: datetime | None = None
    probe_scope: FailureScope | None = None
    probe_key: str | None = None


@dataclass(frozen=True, slots=True)
class AvailabilitySnapshot:
    """Immutable health view used by one routing decision."""

    revision: int
    observed_at: datetime
    target_states: Mapping[str, TargetAvailability]
    earliest_recovery_at: datetime | None


@dataclass(frozen=True, slots=True)
class HealthLease:
    """Admission token binding one attempt to its failure-domain generation."""

    token: int
    provider: str
    target_alias: str
    snapshot_revision: int
    acquired_at: datetime
    probe: bool
    provider_generation: int
    target_generation: int


@dataclass(frozen=True, slots=True)
class AttemptOutcome:
    """Sanitized execution outcome applied to one admitted attempt."""

    failure_class: FailureClass
    observed_at: datetime
    retry_after_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class HealthTransition:
    """Bounded state transition emitted outside the coordinator lock."""

    provider: str
    target_alias: str | None
    from_state: HealthState
    to_state: HealthState
    failure_class: FailureClass | None = None
    cooldown_seconds: float | None = None
