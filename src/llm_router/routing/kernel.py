"""Pure deterministic routing kernel."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from llm_router.domain import (
    Capability,
    ExecutionPlan,
    ModelTarget,
    RoutingRequest,
    Tier,
)
from llm_router.errors import no_available_target, no_capable_model, unknown_model
from llm_router.health.models import (
    AvailabilityReason,
    AvailabilitySnapshot,
    HealthState,
)
from llm_router.routing.context import RoutingContext, SessionSnapshot
from llm_router.routing.policy import RoutingPolicy, compile_routing_policy

if TYPE_CHECKING:
    from llm_router.config import RouterConfig

_TIER_ORDER = (Tier.FAST, Tier.BALANCED, Tier.DEEP)


class RoutingKernel:
    """Build plans from a policy and explicit immutable context only."""

    def __init__(
        self,
        policy: RoutingPolicy | RouterConfig,
    ) -> None:
        """Bind one policy; compile legacy config input immediately."""

        self._policy = compile_routing_policy(policy) if not isinstance(policy, RoutingPolicy) else policy

    @property
    def policy(self) -> RoutingPolicy:
        """Expose the immutable policy for capture and compatibility checks."""

        return self._policy

    def _capable(self, target: ModelTarget, request: RoutingRequest) -> bool:
        """Check hard capabilities and input capacity before ranking."""

        return (
            target.protocol is request.protocol
            and request.required_capabilities.issubset(target.capabilities)
            and request.estimated_input_tokens <= target.max_input_tokens
            and (
                not request.response_state_requested
                or Capability.RESPONSE_STATE in target.capabilities and target.state_scope is not None
            )
            and (
                not request.provider_managed_tools_requested
                or Capability.PROVIDER_MANAGED_TOOLS in target.capabilities
            )
        )

    def _auto_tier(
        self, request: RoutingRequest, state: SessionSnapshot | None
    ) -> tuple[Tier, list[str]]:
        """Apply automatic routing rules in documented priority order."""

        policy = self._policy
        if request.outcome_signal.value == "failure" or (
            state is not None and state.consecutive_failures >= policy.failure_escalation_requests
        ):
            return Tier.DEEP, ["failure_escalation"]
        if request.tool_rounds >= policy.deep_tool_rounds_threshold:
            return Tier.DEEP, ["deep_tool_loop"]
        if request.estimated_input_tokens > policy.balanced_max_input_tokens:
            return Tier.DEEP, ["large_context"]
        complex_capabilities = {
            Capability.TOOLS,
            Capability.THINKING,
            Capability.VISION,
            Capability.REASONING,
            Capability.STRUCTURED_OUTPUT,
            Capability.PROVIDER_MANAGED_TOOLS,
        }
        signals = request.task_signals
        if request.required_capabilities & complex_capabilities or any(
            (signals.complex_planning, signals.debugging, signals.review, signals.multi_file_refactor)
        ):
            return Tier.BALANCED, ["task_capability_or_complexity"]
        if request.estimated_input_tokens <= policy.fast_max_input_tokens:
            return Tier.FAST, ["short_simple_request"]
        return Tier.BALANCED, ["uncertain_default_balanced"]

    def _ordered_auto_targets(self, desired: Tier, request: RoutingRequest) -> list[ModelTarget]:
        """Order capable automatic targets from desired tier upward."""

        result: list[ModelTarget] = []
        for tier in _TIER_ORDER[_TIER_ORDER.index(desired) :]:
            result.extend(
                sorted(
                    (
                        target
                        for target in self._policy.targets.values()
                        if target.tier is tier and self._capable(target, request)
                    ),
                    key=lambda target: target.alias,
                )
            )
        return result

    @staticmethod
    def _available_targets(
        targets: list[ModelTarget], availability: AvailabilitySnapshot
    ) -> tuple[list[ModelTarget], int, str | None]:
        """Filter unavailable targets and deduplicate recovery probes."""

        available: list[ModelTarget] = []
        filtered_reasons: set[AvailabilityReason] = set()
        retained_probe_domains: set[tuple[object, str]] = set()
        for target in targets:
            state = availability.target_states.get(target.alias)
            if state is None or not state.eligible:
                if state is not None:
                    filtered_reasons.add(state.reason)
                continue
            if state.state is HealthState.HALF_OPEN:
                if state.probe_scope is None or state.probe_key is None:
                    continue
                domain = (state.probe_scope, state.probe_key)
                if domain in retained_probe_domains:
                    continue
                retained_probe_domains.add(domain)
            available.append(target)
        filtered_count = len(targets) - len(available)
        if not filtered_count:
            return available, 0, None
        blocked = {AvailabilityReason.PROVIDER_BLOCKED, AvailabilityReason.TARGET_BLOCKED}
        reason = (
            "health_blocked_filtered"
            if filtered_reasons and filtered_reasons.issubset(blocked)
            else "health_cooldown_filtered"
        )
        return available, filtered_count, reason

    @staticmethod
    def _retry_after(availability: AvailabilitySnapshot) -> float | None:
        """Calculate whole recovery seconds from a snapshot."""

        if availability.earliest_recovery_at is None:
            return None
        remaining = (availability.earliest_recovery_at - availability.observed_at).total_seconds()
        return float(max(0, math.ceil(remaining)))

    def plan(
        self,
        request: RoutingRequest,
        context: RoutingContext | AvailabilitySnapshot,
    ) -> ExecutionPlan:
        """Build one deterministic plan without reading mutable stores."""

        if isinstance(context, AvailabilitySnapshot):
            context = RoutingContext(None, context)
        policy = self._policy
        profile_name = request.requested_profile or policy.default_profile
        profile = policy.profiles.get(profile_name)
        if profile is None:
            raise unknown_model()
        auxiliary: list[str] = []
        desired: Tier | None = None
        if profile.automatic:
            desired, reasons = self._auto_tier(request, context.session)
            if context.session is not None and context.session.consecutive_failures:
                auxiliary.append("session_failure_state")
            capable_targets = self._ordered_auto_targets(desired, request)
            route_reason = reasons[0]
        else:
            chain = profile.targets.get(request.protocol)
            if chain is None:
                raise no_capable_model()
            configured = [policy.targets[alias] for alias in (chain.primary, *chain.fallback)]
            capable_targets = [target for target in configured if self._capable(target, request)]
            route_reason = f"explicit_{profile_name.replace('/', '_')}"
            if len(capable_targets) < len(configured):
                auxiliary.append("incapable_target_filtered")
        if not capable_targets:
            raise no_capable_model()
        targets, filtered_count, health_reason = self._available_targets(
            capable_targets, context.availability
        )
        if not targets:
            error = no_available_target(self._retry_after(context.availability))
            error.health_snapshot_revision = context.availability.revision
            error.health_filtered_count = len(capable_targets)
            error.health_reason = health_reason or "health_no_available_target"
            raise error
        if health_reason is not None:
            auxiliary.append(health_reason)
        if desired is not None and targets[0].tier is not desired and any(
            target.tier is desired for target in capable_targets
        ):
            health_reason = "health_tier_fallback"
            auxiliary.append(health_reason)
        if request.response_state_requested:
            original_count = len(targets)
            primary_scope = targets[0].state_scope
            targets = [target for target in targets if target.state_scope == primary_scope]
            if len(targets) < original_count:
                auxiliary.append("state_scope_filtered")
            if len(targets) < 2 or policy.attempt_limit < 2:
                auxiliary.append("stateful_no_cross_scope_fallback")
        if not targets:
            raise no_capable_model()
        primary, *fallbacks = targets
        return ExecutionPlan(
            primary=primary,
            fallbacks=tuple(fallbacks),
            attempt_limit=min(policy.attempt_limit, len(targets)),
            timeouts=policy.timeouts,
            route_reason=route_reason,
            auxiliary_reasons=tuple(auxiliary),
            profile=profile_name,
            policy_version=policy.effective_policy_version,
            health_snapshot_revision=context.availability.revision,
            health_filtered_count=filtered_count,
            health_reason=health_reason,
        )
