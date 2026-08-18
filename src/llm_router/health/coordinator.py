"""Thread-safe in-memory Provider and Model Target health state machine."""

from __future__ import annotations

import itertools
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType

from llm_router.config import HealthConfig
from llm_router.domain import ModelTarget
from llm_router.health.models import (
    AttemptOutcome,
    AvailabilityReason,
    AvailabilitySnapshot,
    FailureClass,
    FailureScope,
    HealthLease,
    HealthState,
    HealthTransition,
    TargetAvailability,
)


TransitionObserver = Callable[[HealthTransition], None]


@dataclass(slots=True)
class _DomainRecord:
    """Mutable state kept only inside the coordinator lock."""

    state: HealthState = HealthState.HEALTHY
    consecutive_failures: int = 0
    window_started_at: datetime | None = None
    cooldown_until: datetime | None = None
    backoff_level: int = 0
    half_open_token: int | None = None
    generation: int = 0


class InMemoryHealthCoordinator:
    """Maintain bounded Provider and Model Target failure domains in memory."""

    def __init__(
        self,
        config: HealthConfig,
        targets: Mapping[str, ModelTarget],
        observer: TransitionObserver | None = None,
    ) -> None:
        """Initialize fixed-capacity domain records from validated targets."""

        self._config = config
        self._targets = dict(targets)
        self._providers = {
            target.provider: _DomainRecord() for target in self._targets.values()
        }
        self._target_records = {alias: _DomainRecord() for alias in self._targets}
        self._observer = observer
        self._lock = threading.Lock()
        self._revision = 0
        self._tokens = itertools.count(1)
        self._active_leases: dict[int, HealthLease] = {}

    @staticmethod
    def _retry_at(record: _DomainRecord, now: datetime) -> datetime | None:
        """Return a future recovery time for one unavailable domain."""

        if record.state is HealthState.COOLDOWN and record.cooldown_until is not None:
            return record.cooldown_until if record.cooldown_until > now else None
        return None

    @staticmethod
    def _effective_state(record: _DomainRecord, now: datetime) -> HealthState:
        """Expose expired cooldown as probe-eligible half-open state."""

        if (
            record.state is HealthState.COOLDOWN
            and record.cooldown_until is not None
            and record.cooldown_until <= now
        ):
            return HealthState.HALF_OPEN
        return record.state

    def _availability(self, target: ModelTarget, now: datetime) -> TargetAvailability:
        """Combine Provider and Target domains into one routing fact."""

        provider = self._providers[target.provider]
        target_record = self._target_records[target.alias]
        provider_state = self._effective_state(provider, now)
        target_state = self._effective_state(target_record, now)
        if provider_state is HealthState.BLOCKED:
            return TargetAvailability(False, provider_state, AvailabilityReason.PROVIDER_BLOCKED)
        if provider_state is HealthState.COOLDOWN or (
            provider_state is HealthState.HALF_OPEN and provider.half_open_token is not None
        ):
            return TargetAvailability(
                False,
                provider_state,
                AvailabilityReason.PROVIDER_COOLDOWN,
                self._retry_at(provider, now),
            )
        if target_state is HealthState.BLOCKED:
            return TargetAvailability(False, target_state, AvailabilityReason.TARGET_BLOCKED)
        if target_state is HealthState.COOLDOWN or (
            target_state is HealthState.HALF_OPEN and target_record.half_open_token is not None
        ):
            return TargetAvailability(
                False,
                target_state,
                AvailabilityReason.TARGET_COOLDOWN,
                self._retry_at(target_record, now),
            )
        if provider_state is HealthState.HALF_OPEN:
            return TargetAvailability(
                True,
                HealthState.HALF_OPEN,
                AvailabilityReason.PROBE_ELIGIBLE,
                probe_scope=FailureScope.PROVIDER,
                probe_key=target.provider,
            )
        if target_state is HealthState.HALF_OPEN:
            return TargetAvailability(
                True,
                HealthState.HALF_OPEN,
                AvailabilityReason.PROBE_ELIGIBLE,
                probe_scope=FailureScope.TARGET,
                probe_key=target.alias,
            )
        return TargetAvailability(True, HealthState.HEALTHY, AvailabilityReason.HEALTHY)

    def snapshot(self, now: datetime) -> AvailabilitySnapshot:
        """Return an immutable availability view without external side effects."""

        with self._lock:
            states = {
                alias: self._availability(target, now)
                for alias, target in self._targets.items()
            }
            recovery_times = [
                state.retry_at for state in states.values() if state.retry_at is not None
            ]
            return AvailabilitySnapshot(
                revision=self._revision,
                observed_at=now,
                target_states=MappingProxyType(states),
                earliest_recovery_at=min(recovery_times, default=None),
            )

    def _transition(
        self,
        record: _DomainRecord,
        provider: str,
        target_alias: str | None,
        state: HealthState,
        failure_class: FailureClass | None,
        cooldown_seconds: float | None = None,
    ) -> HealthTransition | None:
        """Apply one state transition and advance bounded revisions."""

        previous = record.state
        if previous is state:
            return None
        record.state = state
        record.generation += 1
        self._revision += 1
        return HealthTransition(
            provider=provider,
            target_alias=target_alias,
            from_state=previous,
            to_state=state,
            failure_class=failure_class,
            cooldown_seconds=cooldown_seconds,
        )

    def _notify(self, transitions: list[HealthTransition]) -> None:
        """Deliver transitions best-effort after releasing the state lock."""

        if self._observer is None:
            return
        for transition in transitions:
            try:
                self._observer(transition)
            except Exception:
                continue

    def acquire(self, target: ModelTarget, now: datetime) -> HealthLease | None:
        """Atomically admit a normal call or one half-open recovery probe."""

        transitions: list[HealthTransition] = []
        with self._lock:
            configured = self._targets.get(target.alias)
            if configured is None or configured.provider != target.provider:
                return None
            provider_record = self._providers[target.provider]
            target_record = self._target_records[target.alias]
            records = (
                (provider_record, target.provider, None),
                (target_record, target.provider, target.alias),
            )
            effective_states: list[HealthState] = []
            for record, _, _ in records:
                effective = self._effective_state(record, now)
                effective_states.append(effective)
                if effective is HealthState.BLOCKED or effective is HealthState.COOLDOWN:
                    return None
                if effective is HealthState.HALF_OPEN and record.half_open_token is not None:
                    return None
            probe_index = next(
                (
                    index
                    for index, effective in enumerate(effective_states)
                    if effective is HealthState.HALF_OPEN
                ),
                None,
            )
            probe = probe_index is not None
            token = next(self._tokens)
            if probe_index is not None:
                record, provider, alias = records[probe_index]
                transition = self._transition(
                    record,
                    provider,
                    alias,
                    HealthState.HALF_OPEN,
                    None,
                )
                if transition is not None:
                    transitions.append(transition)
                record.half_open_token = token
                record.cooldown_until = None
                if transition is None:
                    self._revision += 1
            lease = HealthLease(
                token=token,
                provider=target.provider,
                target_alias=target.alias,
                snapshot_revision=self._revision,
                acquired_at=now,
                probe=probe,
                provider_generation=provider_record.generation,
                target_generation=target_record.generation,
            )
            self._active_leases[token] = lease
        self._notify(transitions)
        return lease

    def _reset(
        self,
        record: _DomainRecord,
        provider: str,
        target_alias: str | None,
    ) -> HealthTransition | None:
        """Reset one non-stale failure domain after a successful attempt."""

        previous = record.state
        changed = (
            previous is not HealthState.HEALTHY
            or record.consecutive_failures > 0
            or record.window_started_at is not None
            or record.backoff_level > 0
        )
        record.state = HealthState.HEALTHY
        record.consecutive_failures = 0
        record.window_started_at = None
        record.cooldown_until = None
        record.backoff_level = 0
        record.half_open_token = None
        if not changed:
            return None
        record.generation += 1
        self._revision += 1
        if previous is HealthState.HEALTHY:
            return None
        return HealthTransition(provider, target_alias, previous, HealthState.HEALTHY, FailureClass.SUCCESS)

    def _cooldown_seconds(
        self, record: _DomainRecord, retry_after_seconds: float | None
    ) -> float:
        """Calculate capped exponential cooldown for one domain."""

        exponential = self._config.cooldown_seconds * (
            self._config.backoff_multiplier ** record.backoff_level
        )
        requested = max(0.0, retry_after_seconds or 0.0)
        return min(self._config.max_cooldown_seconds, max(exponential, requested))

    def _transient_failure(
        self,
        record: _DomainRecord,
        provider: str,
        target_alias: str | None,
        outcome: AttemptOutcome,
        lease_token: int,
    ) -> HealthTransition | None:
        """Apply failure-window threshold or half-open probe backoff."""

        half_open_probe = (
            record.state is HealthState.HALF_OPEN and record.half_open_token == lease_token
        )
        if half_open_probe:
            threshold_reached = True
        else:
            window = timedelta(seconds=self._config.failure_window_seconds)
            if record.window_started_at is None or outcome.observed_at - record.window_started_at > window:
                record.window_started_at = outcome.observed_at
                record.consecutive_failures = 0
            record.consecutive_failures += 1
            threshold_reached = record.consecutive_failures >= self._config.failure_threshold
        record.half_open_token = None
        if not threshold_reached:
            return None
        duration = self._cooldown_seconds(record, outcome.retry_after_seconds)
        record.cooldown_until = outcome.observed_at + timedelta(seconds=duration)
        record.consecutive_failures = 0
        record.window_started_at = None
        record.backoff_level += 1
        return self._transition(
            record,
            provider,
            target_alias,
            HealthState.COOLDOWN,
            outcome.failure_class,
            duration,
        )

    def _permanent_failure(
        self,
        record: _DomainRecord,
        provider: str,
        target_alias: str | None,
        failure_class: FailureClass,
    ) -> HealthTransition | None:
        """Block one failure domain until coordinator reconstruction."""

        record.cooldown_until = None
        record.half_open_token = None
        record.consecutive_failures = 0
        record.window_started_at = None
        return self._transition(
            record,
            provider,
            target_alias,
            HealthState.BLOCKED,
            failure_class,
        )

    def record(self, lease: HealthLease, outcome: AttemptOutcome) -> None:
        """Apply one non-stale sanitized outcome at most once."""

        transitions: list[HealthTransition] = []
        with self._lock:
            active = self._active_leases.pop(lease.token, None)
            if active != lease:
                return
            provider_record = self._providers.get(lease.provider)
            target_record = self._target_records.get(lease.target_alias)
            if provider_record is None or target_record is None:
                return
            provider_current = provider_record.generation == lease.provider_generation
            target_current = target_record.generation == lease.target_generation
            failure_class = outcome.failure_class
            if failure_class is FailureClass.SUCCESS:
                if provider_current:
                    transition = self._reset(provider_record, lease.provider, None)
                    if transition is not None:
                        transitions.append(transition)
                if target_current:
                    transition = self._reset(target_record, lease.provider, lease.target_alias)
                    if transition is not None:
                        transitions.append(transition)
            elif failure_class in {
                FailureClass.PROVIDER_TRANSIENT,
                FailureClass.POST_COMMIT_STREAM_FAILURE,
            } and provider_current:
                transition = self._transient_failure(
                    provider_record, lease.provider, None, outcome, lease.token
                )
                if transition is not None:
                    transitions.append(transition)
            elif failure_class is FailureClass.TARGET_TRANSIENT and target_current:
                transition = self._transient_failure(
                    target_record, lease.provider, lease.target_alias, outcome, lease.token
                )
                if transition is not None:
                    transitions.append(transition)
            elif failure_class is FailureClass.PROVIDER_PERMANENT and provider_current:
                transition = self._permanent_failure(
                    provider_record, lease.provider, None, failure_class
                )
                if transition is not None:
                    transitions.append(transition)
            elif failure_class is FailureClass.TARGET_PERMANENT and target_current:
                transition = self._permanent_failure(
                    target_record, lease.provider, lease.target_alias, failure_class
                )
                if transition is not None:
                    transitions.append(transition)
            else:
                if provider_current and provider_record.half_open_token == lease.token:
                    provider_record.half_open_token = None
                    self._revision += 1
                if target_current and target_record.half_open_token == lease.token:
                    target_record.half_open_token = None
                    self._revision += 1
        self._notify(transitions)


class DisabledHealthCoordinator:
    """Preserve v0.2 behavior behind the same HealthPort interface."""

    def __init__(self, targets: Mapping[str, ModelTarget]) -> None:
        """Store the fixed target registry used for all-healthy snapshots."""

        self._targets = dict(targets)
        self._tokens = itertools.count(1)

    def snapshot(self, now: datetime) -> AvailabilitySnapshot:
        """Return an immutable all-healthy snapshot."""

        states = {
            alias: TargetAvailability(True, HealthState.HEALTHY, AvailabilityReason.HEALTHY)
            for alias in self._targets
        }
        return AvailabilitySnapshot(0, now, MappingProxyType(states), None)

    def acquire(self, target: ModelTarget, now: datetime) -> HealthLease | None:
        """Always admit a configured target with a normal lease."""

        configured = self._targets.get(target.alias)
        if configured is None or configured.provider != target.provider:
            return None
        return HealthLease(next(self._tokens), target.provider, target.alias, 0, now, False, 0, 0)

    def record(self, lease: HealthLease, outcome: AttemptOutcome) -> None:
        """Ignore outcomes while health management is disabled."""
