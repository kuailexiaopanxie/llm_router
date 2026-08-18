"""Deterministic state-machine tests for in-memory health coordination."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from conftest import router_config_data

from llm_router.config import HealthConfig, RouterConfig
from llm_router.domain import ModelTarget
from llm_router.health.coordinator import (
    DisabledHealthCoordinator,
    InMemoryHealthCoordinator,
)
from llm_router.health.models import AttemptOutcome, FailureClass, HealthState

NOW = datetime(2026, 8, 18, tzinfo=UTC)


def _targets() -> dict[str, ModelTarget]:
    """Return the fixed dual-protocol Model Target registry."""

    return RouterConfig.model_validate(router_config_data()).model_targets()


def _record(
    coordinator: InMemoryHealthCoordinator,
    target: ModelTarget,
    failure_class: FailureClass,
    now: datetime,
    retry_after: float | None = None,
) -> None:
    """Acquire and apply one deterministic attempt outcome."""

    lease = coordinator.acquire(target, now)
    assert lease is not None
    coordinator.record(lease, AttemptOutcome(failure_class, now, retry_after))


def test_threshold_cooldown_and_provider_scope() -> None:
    """Suppress every target under a Provider only after the threshold."""

    targets = _targets()
    coordinator = InMemoryHealthCoordinator(HealthConfig(), targets)
    _record(coordinator, targets["openai_fast"], FailureClass.PROVIDER_TRANSIENT, NOW)
    assert coordinator.snapshot(NOW).target_states["openai_fast"].eligible

    _record(
        coordinator,
        targets["openai_fast"],
        FailureClass.PROVIDER_TRANSIENT,
        NOW + timedelta(seconds=1),
    )
    snapshot = coordinator.snapshot(NOW + timedelta(seconds=2))

    assert not snapshot.target_states["openai_fast"].eligible
    assert not snapshot.target_states["openai_balanced"].eligible
    assert snapshot.target_states["anthropic_fast"].eligible


def test_half_open_admits_exactly_one_concurrent_probe() -> None:
    """Admit one recovery probe when 100 callers race after cooldown."""

    targets = _targets()
    config = HealthConfig(failure_threshold=1, cooldown_seconds=10, max_cooldown_seconds=40)
    coordinator = InMemoryHealthCoordinator(config, targets)
    target = targets["openai_fast"]
    _record(coordinator, target, FailureClass.TARGET_TRANSIENT, NOW)
    probe_time = NOW + timedelta(seconds=10)

    with ThreadPoolExecutor(max_workers=100) as pool:
        leases = list(pool.map(lambda _: coordinator.acquire(target, probe_time), range(100)))

    admitted = [lease for lease in leases if lease is not None]
    assert len(admitted) == 1
    assert admitted[0].probe is True
    coordinator.record(admitted[0], AttemptOutcome(FailureClass.SUCCESS, probe_time))
    assert coordinator.snapshot(probe_time).target_states[target.alias].state is HealthState.HEALTHY


def test_probe_failure_backoff_and_retry_after_are_capped() -> None:
    """Increase probe cooldown exponentially and cap Retry-After."""

    targets = _targets()
    config = HealthConfig(
        failure_threshold=1,
        cooldown_seconds=10,
        max_cooldown_seconds=40,
        backoff_multiplier=2,
    )
    coordinator = InMemoryHealthCoordinator(config, targets)
    target = targets["openai_fast"]
    _record(coordinator, target, FailureClass.TARGET_TRANSIENT, NOW)
    probe_time = NOW + timedelta(seconds=10)
    probe = coordinator.acquire(target, probe_time)
    assert probe is not None
    coordinator.record(
        probe,
        AttemptOutcome(FailureClass.TARGET_TRANSIENT, probe_time, retry_after_seconds=1000),
    )

    snapshot = coordinator.snapshot(probe_time)
    assert snapshot.earliest_recovery_at == probe_time + timedelta(seconds=40)


def test_permanent_failures_respect_provider_and_target_domains() -> None:
    """Block only the classified Provider or Model Target domain."""

    targets = _targets()
    provider_health = InMemoryHealthCoordinator(HealthConfig(), targets)
    _record(provider_health, targets["openai_fast"], FailureClass.PROVIDER_PERMANENT, NOW)
    provider_snapshot = provider_health.snapshot(NOW)
    assert not provider_snapshot.target_states["openai_fast"].eligible
    assert not provider_snapshot.target_states["openai_deep"].eligible
    assert provider_snapshot.target_states["anthropic_fast"].eligible

    target_health = InMemoryHealthCoordinator(HealthConfig(), targets)
    _record(target_health, targets["openai_fast"], FailureClass.TARGET_PERMANENT, NOW)
    target_snapshot = target_health.snapshot(NOW)
    assert not target_snapshot.target_states["openai_fast"].eligible
    assert target_snapshot.target_states["openai_balanced"].eligible


def test_neutral_and_duplicate_outcomes_do_not_change_health() -> None:
    """Ignore cancellation, request rejection, and duplicate lease outcomes."""

    targets = _targets()
    coordinator = InMemoryHealthCoordinator(HealthConfig(failure_threshold=1), targets)
    target = targets["openai_fast"]
    lease = coordinator.acquire(target, NOW)
    assert lease is not None
    coordinator.record(lease, AttemptOutcome(FailureClass.CLIENT_CANCELLED, NOW))
    coordinator.record(lease, AttemptOutcome(FailureClass.TARGET_PERMANENT, NOW))

    assert coordinator.snapshot(NOW).target_states[target.alias].eligible


def test_disabled_coordinator_is_always_healthy() -> None:
    """Preserve v0.2 admission and ignore every outcome when disabled."""

    targets = _targets()
    coordinator = DisabledHealthCoordinator(targets)
    target = targets["openai_fast"]
    lease = coordinator.acquire(target, NOW)
    assert lease is not None
    coordinator.record(lease, AttemptOutcome(FailureClass.PROVIDER_PERMANENT, NOW))

    snapshot = coordinator.snapshot(NOW + timedelta(days=1))
    assert all(state.eligible for state in snapshot.target_states.values())
    assert snapshot.revision == 0


def test_provider_probe_does_not_hold_expired_target_probe_token() -> None:
    """Bind one lease to the Provider probe when both domains expire together."""

    targets = _targets()
    config = HealthConfig(
        failure_threshold=1,
        cooldown_seconds=10,
        max_cooldown_seconds=40,
    )
    coordinator = InMemoryHealthCoordinator(config, targets)
    fast = targets["openai_fast"]
    balanced = targets["openai_balanced"]
    _record(coordinator, fast, FailureClass.TARGET_TRANSIENT, NOW)
    _record(coordinator, balanced, FailureClass.PROVIDER_TRANSIENT, NOW)

    first_probe_at = NOW + timedelta(seconds=10)
    provider_probe = coordinator.acquire(fast, first_probe_at)
    assert provider_probe is not None
    coordinator.record(
        provider_probe,
        AttemptOutcome(FailureClass.PROVIDER_TRANSIENT, first_probe_at),
    )

    provider_recovers_at = first_probe_at + timedelta(seconds=20)
    recovery_probe = coordinator.acquire(balanced, provider_recovers_at)
    assert recovery_probe is not None
    coordinator.record(
        recovery_probe,
        AttemptOutcome(FailureClass.SUCCESS, provider_recovers_at),
    )

    target_probe = coordinator.acquire(fast, provider_recovers_at)
    assert target_probe is not None
    assert target_probe.probe is True
