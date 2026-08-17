"""Pure deterministic routing policy."""

from __future__ import annotations

from llm_router.config import AutoProfileConfig, ExplicitProfileConfig, RouterConfig
from llm_router.domain import Capability, ExecutionPlan, ModelTarget, RoutingRequest, Tier
from llm_router.gateway.errors import no_capable_model, unknown_model
from llm_router.routing.session import SessionState, SessionStateStore


_TIER_ORDER = (Tier.FAST, Tier.BALANCED, Tier.DEEP)


class RoutingKernel:
    """Compile one immutable execution plan from request features and policy."""

    def __init__(self, config: RouterConfig, sessions: SessionStateStore) -> None:
        self._config = config
        self._targets = config.model_targets()
        self._sessions = sessions
        self._timeouts = config.timeouts.to_domain()

    def _capable(self, target: ModelTarget, request: RoutingRequest) -> bool:
        """Check all hard capabilities and input capacity before ranking."""

        return (
            target.protocol is request.protocol
            and request.required_capabilities.issubset(target.capabilities)
            and request.estimated_input_tokens <= target.max_input_tokens
            and (
                not request.response_state_requested
                or Capability.RESPONSE_STATE in target.capabilities
                and target.state_scope is not None
            )
            and (
                not request.provider_managed_tools_requested
                or Capability.PROVIDER_MANAGED_TOOLS in target.capabilities
            )
        )

    def _auto_tier(self, request: RoutingRequest, state: SessionState | None) -> tuple[Tier, list[str]]:
        """Apply documented auto rules in priority order."""

        reasons: list[str] = []
        if request.outcome_signal.value == "failure" or (
            state is not None and state.consecutive_failures >= self._config.routing.failure_escalation_requests
        ):
            reasons.append("failure_escalation")
            return Tier.DEEP, reasons
        if request.tool_rounds >= self._config.routing.deep_tool_rounds_threshold:
            reasons.append("deep_tool_loop")
            return Tier.DEEP, reasons
        if request.estimated_input_tokens > self._config.routing.balanced_max_input_tokens:
            reasons.append("large_context")
            return Tier.DEEP, reasons
        if (
            request.required_capabilities
            & {
                Capability.TOOLS,
                Capability.THINKING,
                Capability.VISION,
                Capability.REASONING,
                Capability.STRUCTURED_OUTPUT,
                Capability.PROVIDER_MANAGED_TOOLS,
            }
            or request.task_signals.complex_planning
            or request.task_signals.debugging
            or request.task_signals.review
            or request.task_signals.multi_file_refactor
        ):
            reasons.append("task_capability_or_complexity")
            return Tier.BALANCED, reasons
        if request.estimated_input_tokens <= self._config.routing.fast_max_input_tokens:
            reasons.append("short_simple_request")
            return Tier.FAST, reasons
        reasons.append("uncertain_default_balanced")
        return Tier.BALANCED, reasons

    def _ordered_auto_targets(self, desired: Tier, request: RoutingRequest) -> list[ModelTarget]:
        """Order capable automatic targets from desired tier upward."""

        start = _TIER_ORDER.index(desired)
        result: list[ModelTarget] = []
        for tier in _TIER_ORDER[start:]:
            result.extend(
                sorted(
                    (
                        target
                        for target in self._targets.values()
                        if target.tier is tier and self._capable(target, request)
                    ),
                    key=lambda target: target.alias,
                )
            )
        return result

    def plan(self, request: RoutingRequest) -> ExecutionPlan:
        """Build an immutable execution plan without network or persistence side effects."""

        profile_name = request.requested_profile or self._config.routing.default_profile
        profile = self._config.profiles.get(profile_name)
        if profile is None:
            raise unknown_model()
        state = self._sessions.snapshot(request.session_id)
        auxiliary: list[str] = []
        if isinstance(profile, AutoProfileConfig):
            desired, reasons = self._auto_tier(request, state)
            if state is not None and state.consecutive_failures:
                auxiliary.append("session_failure_state")
            targets = self._ordered_auto_targets(desired, request)
            route_reason = reasons[0]
        else:
            assert isinstance(profile, ExplicitProfileConfig)
            chain = profile.chain_for(request.protocol)
            if chain is None:
                raise no_capable_model()
            configured = [self._targets[alias] for alias in (chain.primary, *chain.fallback)]
            targets = [target for target in configured if self._capable(target, request)]
            route_reason = f"explicit_{profile_name.replace('/', '_')}"
            if not targets:
                raise no_capable_model()
            if len(targets) < len(configured):
                auxiliary.append("incapable_target_filtered")
        if request.response_state_requested:
            primary_scope = targets[0].state_scope
            scoped_targets = [target for target in targets if target.state_scope == primary_scope]
            if len(scoped_targets) < len(targets):
                auxiliary.append("state_scope_filtered")
            targets = scoped_targets
            if len(targets) < 2 or self._config.routing.attempt_limit < 2:
                auxiliary.append("stateful_no_cross_scope_fallback")
        if not targets:
            raise no_capable_model()
        primary, *fallbacks = targets
        return ExecutionPlan(
            primary=primary,
            fallbacks=tuple(fallbacks),
            attempt_limit=min(self._config.routing.attempt_limit, len(targets)),
            timeouts=self._timeouts,
            route_reason=route_reason,
            auxiliary_reasons=tuple(auxiliary),
            profile=profile_name,
            policy_version=self._config.effective_policy_version,
        )
