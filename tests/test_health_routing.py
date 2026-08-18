"""Deterministic routing tests for immutable health snapshots."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from llm_router.config import HealthConfig, RouterConfig
from llm_router.domain import ModelTarget, Protocol
from llm_router.gateway.errors import RouterError
from llm_router.health.coordinator import InMemoryHealthCoordinator
from llm_router.health.models import AttemptOutcome, FailureClass
from llm_router.routing.features import extract_routing_request as anthropic_features
from llm_router.routing.kernel import RoutingKernel
from llm_router.routing.openai_features import (
    extract_routing_request as openai_features,
)
from llm_router.routing.policy import compile_routing_policy


def _kernel(config: RouterConfig) -> RoutingKernel:
    """Build a routing kernel without prior session state."""

    return RoutingKernel(compile_routing_policy(config))


def _fail_target(
    health: InMemoryHealthCoordinator,
    target: ModelTarget,
    failure_class: FailureClass,
    now: datetime,
) -> None:
    """Apply one admitted failure to a configured target."""

    lease = health.acquire(target, now)
    assert lease is not None
    health.record(lease, AttemptOutcome(failure_class, now))


def test_target_cooldown_preserves_protocol_and_uses_tier_fallback(
    router_config: RouterConfig,
) -> None:
    """Filter one unhealthy target without crossing protocol boundaries."""

    now = datetime.now(timezone.utc)
    targets = router_config.model_targets()
    health = InMemoryHealthCoordinator(
        HealthConfig(failure_threshold=1),
        targets,
    )
    _fail_target(health, targets["anthropic_fast"], FailureClass.TARGET_TRANSIENT, now)
    request = anthropic_features(
        {"messages": [{"role": "user", "content": "hello"}]},
        "code/auto",
    )

    plan = _kernel(router_config).plan(request, health.snapshot(now))

    assert plan.primary.alias == "anthropic_balanced"
    assert plan.health_filtered_count == 1
    assert plan.health_reason == "health_tier_fallback"
    assert all(target.protocol is Protocol.ANTHROPIC_MESSAGES for target in plan.targets)


def test_all_capable_targets_unavailable_returns_503(router_config: RouterConfig) -> None:
    """Distinguish temporary unavailability from capability mismatch."""

    now = datetime.now(timezone.utc)
    targets = router_config.model_targets()
    health = InMemoryHealthCoordinator(HealthConfig(), targets)
    _fail_target(
        health,
        targets["openai_fast"],
        FailureClass.PROVIDER_PERMANENT,
        now,
    )
    request = openai_features({"input": "hello"}, "code/auto")

    with pytest.raises(RouterError) as captured:
        _kernel(router_config).plan(request, health.snapshot(now))

    assert captured.value.code == "router_no_available_target"
    assert captured.value.http_status == 503


def test_health_filter_does_not_remove_capability_requirements(
    router_config: RouterConfig,
) -> None:
    """Return 422 when no target ever had the requested hard capability."""

    now = datetime.now(timezone.utc)
    health = InMemoryHealthCoordinator(router_config.health, router_config.model_targets())
    request = anthropic_features(
        {
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"name": "search", "input_schema": {"type": "object"}}],
            "thinking": {"type": "enabled", "budget_tokens": 1024},
        },
        "code/fast",
    )

    plan = _kernel(router_config).plan(request, health.snapshot(now))

    assert plan.primary.alias == "anthropic_balanced"
    assert "incapable_target_filtered" in plan.auxiliary_reasons
