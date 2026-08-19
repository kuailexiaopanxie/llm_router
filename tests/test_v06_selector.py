"""Deterministic HMAC Canary policy selection tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from conftest import router_config_data

from llm_router.config import RouterConfig
from llm_router.evaluation.canary_models import AffinityKind, CanaryReason, PolicyRole
from llm_router.evaluation.codec import make_policy_snapshot
from llm_router.routing.canary import CanaryPolicySelector, CanaryRuntimeState
from llm_router.routing.candidate import CandidateBundle
from llm_router.routing.coordinator import RoutingInvocation
from llm_router.routing.features import extract_routing_request
from llm_router.routing.kernel import RoutingKernel
from llm_router.routing.policy import compile_routing_policy


def _selector(
    threshold: int,
    state: CanaryRuntimeState | None = None,
) -> CanaryPolicySelector:
    """Build an active self-candidate selector with one eligible segment."""

    config = RouterConfig.model_validate(router_config_data())
    policy = compile_routing_policy(config)
    kernel = RoutingKernel(policy)
    bundle = CandidateBundle(
        config,
        policy,
        kernel,
        make_policy_snapshot(config, datetime.now(UTC)),
        policy.routing_policy_hash,
        True,
        True,
    )
    return CanaryPolicySelector(
        kernel,
        bundle,
        state or CanaryRuntimeState(True, None),
        policy.routing_policy_hash,
        b"s" * 32,
        threshold,
        frozenset({(extract_routing_request({}, "code/auto").protocol, "code/auto")}),
    )


def _invocation(session: str | None = "session-5", count_only: bool = False) -> RoutingInvocation:
    """Build a fixed Anthropic invocation for deterministic assignment."""

    request = extract_routing_request({"messages": []}, "code/auto", count_only=count_only)
    return RoutingInvocation(
        UUID("00000000-0000-4000-8000-000000000001"),
        UUID("00000000-0000-4000-8000-000000000002"),
        session,
        datetime.now(UTC),
        request,
    )


def test_selector_uses_fixed_hmac_vector_and_monotonic_threshold() -> None:
    """Keep a stable bucket and only expand the Canary cohort as rate rises."""

    low = _selector(100).select(_invocation()).assignment
    high = _selector(500).select(_invocation()).assignment
    assert low is not None and high is not None
    assert low.bucket == high.bucket == 222
    assert low.role is PolicyRole.CONTROL
    assert high.role is PolicyRole.CANARY
    assert high.affinity_kind is AffinityKind.SESSION


def test_selector_affinity_priority_and_count_only_exclusion() -> None:
    """Prefer session over task and keep count-only requests on Control."""

    selected = _selector(2500).select(_invocation()).assignment
    excluded = _selector(2500).select(_invocation(count_only=True)).assignment
    assert selected is not None and selected.affinity_kind is AffinityKind.SESSION
    assert excluded is not None
    assert excluded.role is PolicyRole.CONTROL
    assert excluded.reason is CanaryReason.COUNT_ONLY_EXCLUDED


def test_inactive_selector_fails_open_with_bounded_reason() -> None:
    """Return Current without hashing affinity when the startup gate is inactive."""

    selector = _selector(100, CanaryRuntimeState(False, CanaryReason.SHADOW_GATE_NOT_MET))
    assignment = selector.select(_invocation()).assignment
    assert assignment is not None
    assert assignment.role is PolicyRole.CONTROL
    assert assignment.reason is CanaryReason.SHADOW_GATE_NOT_MET
    assert assignment.bucket is None


def test_assignment_rejects_crossed_role_and_bucket_reason() -> None:
    """Prevent persisted role/reason combinations that could corrupt reports."""

    from llm_router.evaluation.canary_models import CanaryAssignment

    with pytest.raises(ValueError, match="canary_bucket"):
        CanaryAssignment(
            PolicyRole.CONTROL,
            CanaryReason.CANARY_BUCKET,
            "a" * 64,
            "a" * 64,
            AffinityKind.REQUEST,
            1,
            100,
        )
