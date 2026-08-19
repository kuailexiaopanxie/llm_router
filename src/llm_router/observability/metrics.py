"""Bounded-label Prometheus metrics for router operations."""

from __future__ import annotations

from collections.abc import Mapping

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from llm_router.domain import ModelTarget
from llm_router.evaluation.canary_models import CanaryAssignment, PolicyRole
from llm_router.health.models import AvailabilitySnapshot, HealthState, HealthTransition
from llm_router.observability.models import ObservationBundle


class RouterMetrics:
    """Keep metrics labels limited to configured route dimensions."""

    def __init__(self) -> None:
        """Create one private Prometheus registry and bounded metric catalog."""

        self.registry = CollectorRegistry()
        self.requests = Counter(
            "llm_router_requests_total",
            "Requests handled",
            ["protocol", "profile", "status", "stage"],
            registry=self.registry,
        )
        self.routes = Counter(
            "llm_router_routes_total", "Requests by final model", ["profile", "tier", "model"], registry=self.registry
        )
        self.attempts = Counter(
            "llm_router_attempts_total",
            "Provider attempts",
            ["protocol", "provider", "model", "status"],
            registry=self.registry,
        )
        self.routing_latency = Histogram(
            "llm_router_total_latency_ms", "End-to-end latency", registry=self.registry
        )
        self.first_event_latency = Histogram(
            "llm_router_first_event_latency_ms", "Time to first stream event", registry=self.registry
        )
        self.first_event_duration = Histogram(
            "llm_router_first_event_duration_seconds",
            "Time to first stream event",
            ["protocol", "model"],
            registry=self.registry,
        )
        self.active_streams = Gauge(
            "llm_router_active_streams", "Active SSE streams", ["protocol"], registry=self.registry
        )
        self.telemetry_dropped = Counter(
            "llm_router_telemetry_dropped_total", "Dropped telemetry events", registry=self.registry
        )
        self.health_state = Gauge(
            "llm_router_health_state",
            "Current one-hot health state",
            ["protocol", "provider", "target", "state"],
            registry=self.registry,
        )
        self.health_cooldown_transitions = Counter(
            "llm_router_health_cooldown_transitions_total",
            "Health domains entering cooldown",
            ["protocol", "provider", "target", "failure_class"],
            registry=self.registry,
        )
        self.health_cooldown_seconds = Histogram(
            "llm_router_health_cooldown_seconds",
            "Applied health cooldown duration",
            ["protocol", "provider", "target"],
            registry=self.registry,
        )
        self.health_probes = Counter(
            "llm_router_health_probes_total",
            "Recovery probes admitted",
            ["protocol", "provider", "target"],
            registry=self.registry,
        )
        self.health_recoveries = Counter(
            "llm_router_health_recoveries_total",
            "Recovery probe outcomes",
            ["protocol", "provider", "target", "outcome"],
            registry=self.registry,
        )
        self.health_skipped = Counter(
            "llm_router_health_skipped_total",
            "Targets skipped by health admission",
            ["protocol", "provider", "target"],
            registry=self.registry,
        )
        self.no_available_target = Counter(
            "llm_router_no_available_target_total",
            "Requests with no available target",
            ["protocol", "profile"],
            registry=self.registry,
        )
        self.health_update_failures = Counter(
            "llm_router_health_update_failures_total",
            "Best-effort health observer update failures",
            registry=self.registry,
        )
        self.outcomes = Counter(
            "llm_router_outcomes_total",
            "Outcome submissions by bounded domain fields",
            ["verdict", "evidence", "source", "status", "correlation"],
            registry=self.registry,
        )
        self.outcome_rejected = Counter(
            "llm_router_outcome_rejected_total",
            "Rejected Outcome submissions",
            ["reason"],
            registry=self.registry,
        )
        self.decision_capture = Counter(
            "llm_router_decision_capture_total",
            "Decision capture lifecycle outcomes",
            ["status"],
            registry=self.registry,
        )
        self.shadow_admission = Counter(
            "llm_router_shadow_admission_total",
            "Shadow admission outcomes",
            ["status"],
            registry=self.registry,
        )
        self.shadow_evaluation = Counter(
            "llm_router_shadow_evaluation_total",
            "Shadow evaluation outcomes",
            ["status"],
            registry=self.registry,
        )
        self.shadow_change = Counter(
            "llm_router_shadow_change_total",
            "Shadow structural changes",
            ["change"],
            registry=self.registry,
        )
        self.shadow_persistence = Counter(
            "llm_router_shadow_persistence_total",
            "Shadow persistence outcomes",
            ["status"],
            registry=self.registry,
        )
        self.shadow_queue_depth = Gauge(
            "llm_router_shadow_queue_depth",
            "Current shadow queue depth",
            registry=self.registry,
        )
        self.shadow_evaluation_duration = Histogram(
            "llm_router_shadow_evaluation_duration_seconds",
            "Shadow evaluation duration",
            registry=self.registry,
        )
        self.canary_runtime_state = Gauge(
            "llm_router_canary_runtime_state",
            "Startup-fixed Canary runtime state",
            ["state"],
            registry=self.registry,
        )
        self.canary_assignment = Counter(
            "llm_router_canary_assignment_total",
            "Canary assignments by bounded role and reason",
            ["role", "reason"],
            registry=self.registry,
        )
        self.canary_routing = Counter(
            "llm_router_canary_routing_total",
            "Selected policy routing results",
            ["role", "result"],
            registry=self.registry,
        )
        self.canary_fail_open = Counter(
            "llm_router_canary_fail_open_total",
            "Canary control-plane fail-open reasons",
            ["reason"],
            registry=self.registry,
        )
        self.canary_decision_capture_gap = Counter(
            "llm_router_canary_decision_capture_gap_total",
            "Canary assignments not admitted for decision capture",
            registry=self.registry,
        )
        self.request_duration = Histogram(
            "llm_router_request_duration_seconds",
            "Terminal request duration",
            ["protocol", "profile", "status"],
            registry=self.registry,
        )
        self.routing_duration = Histogram(
            "llm_router_routing_duration_seconds",
            "Routing duration",
            ["protocol", "profile", "result"],
            registry=self.registry,
        )
        self.inflight_requests = Gauge(
            "llm_router_inflight_requests",
            "Requests currently inside model endpoints",
            ["protocol"],
            registry=self.registry,
        )
        self.fallbacks = Counter(
            "llm_router_fallback_total",
            "Requests using a fallback target",
            ["protocol", "profile"],
            registry=self.registry,
        )
        self.attempt_duration = Histogram(
            "llm_router_attempt_duration_seconds",
            "Provider attempt duration",
            ["protocol", "provider", "model", "status"],
            registry=self.registry,
        )
        self.tokens = Counter(
            "llm_router_tokens_total",
            "Provider-reported normalized token usage",
            ["protocol", "provider", "model", "kind"],
            registry=self.registry,
        )
        self.usage_observations = Counter(
            "llm_router_usage_observations_total",
            "Normalized usage coverage",
            ["protocol", "status"],
            registry=self.registry,
        )
        self.known_estimated_cost = Counter(
            "llm_router_known_estimated_cost_total",
            "Known estimated currency units",
            ["currency", "provider", "model"],
            registry=self.registry,
        )
        self.cost_observations = Counter(
            "llm_router_cost_observations_total",
            "Estimated cost coverage",
            ["currency", "status"],
            registry=self.registry,
        )
        self.observation_queue_depth = Gauge(
            "llm_router_observation_queue_depth",
            "Current durable observation queue depth",
            registry=self.registry,
        )
        self.observation_dropped = Counter(
            "llm_router_observation_dropped_total",
            "Dropped terminal observations",
            ["reason"],
            registry=self.registry,
        )
        self.observation_sink_failures = Counter(
            "llm_router_observation_sink_failures_total",
            "Observation sink failures",
            ["sink", "reason"],
            registry=self.registry,
        )
        self.trace_export = Counter(
            "llm_router_trace_export_total",
            "Trace export outcomes",
            ["exporter", "status"],
            registry=self.registry,
        )
        self.observation_store_size = Gauge(
            "llm_router_observation_store_size_bytes",
            "Observation SQLite size",
            registry=self.registry,
        )
        self.duplicate_terminal = Counter(
            "llm_router_observation_duplicate_terminal_total",
            "Ignored duplicate terminal lifecycle completions",
            registry=self.registry,
        )
        self._targets_by_provider: dict[str, tuple[tuple[str, str], ...]] = {}
        self._target_tiers: dict[str, str] = {}

    def record_canary_runtime(self, state: str, reason: str | None) -> None:
        """Set one startup-fixed state and count bounded fail-open activation."""

        for candidate in ("disabled", "active", "inactive"):
            self.canary_runtime_state.labels(candidate).set(1 if candidate == state else 0)
        if state == "inactive" and reason is not None:
            self.canary_fail_open.labels(reason).inc()

    def record_canary_resolution(
        self,
        assignment: CanaryAssignment | None,
        result: str,
        captured: bool,
    ) -> None:
        """Record one bounded selected-policy routing result."""

        role = assignment.role if assignment is not None else PolicyRole.CONTROL
        if assignment is not None:
            self.canary_assignment.labels(role.value, assignment.reason.value).inc()
            if not captured:
                self.canary_decision_capture_gap.inc()
        self.canary_routing.labels(role.value, result).inc()

    def record_observation(self, bundle: ObservationBundle) -> None:
        """Update complete bounded route, usage, cost, and attempt metrics."""

        event = bundle.observation
        protocol = event.protocol.value if event.protocol else "unknown"
        profile = event.profile or "unknown"
        duration = (event.completed_at - event.received_at).total_seconds()
        self.requests.labels(
            protocol, profile, event.status.value, event.terminal_stage.value
        ).inc()
        self.request_duration.labels(protocol, profile, event.status.value).observe(duration)
        self.routing_latency.observe(duration * 1000)
        if event.routing is not None:
            self.routing_duration.labels(
                protocol, profile, event.routing.result
            ).observe(event.routing.duration_ms / 1000)
        execution = event.execution
        provider = execution.final_provider if execution and execution.final_provider else "unknown"
        model = execution.final_target if execution and execution.final_target else "unknown"
        if execution is not None:
            if execution.final_target is not None:
                self.routes.labels(
                    profile,
                    self._target_tiers.get(execution.final_target, "unknown"),
                    execution.final_target,
                ).inc()
            if execution.time_to_first_event_ms is not None:
                self.first_event_latency.observe(execution.time_to_first_event_ms)
                self.first_event_duration.labels(protocol, model).observe(
                    execution.time_to_first_event_ms / 1000
                )
            if execution.final_target and event.routing and execution.final_target != event.routing.primary_model:
                self.fallbacks.labels(protocol, profile).inc()
            for attempt in execution.attempts:
                self.attempts.labels(
                    protocol, attempt.provider, attempt.model, attempt.status
                ).inc()
                self.attempt_duration.labels(
                    protocol, attempt.provider, attempt.model, attempt.status
                ).observe(attempt.duration_ms / 1000)
                if not attempt.upstream_invoked:
                    self.health_skipped.labels(
                        protocol, attempt.provider, attempt.model
                    ).inc()
        if event.error_code == "router_no_available_target":
            self.no_available_target.labels(protocol, profile).inc()
        self.usage_observations.labels(protocol, event.usage.status.value).inc()
        for kind, tokens in (
            ("input_uncached", event.usage.input_uncached_tokens),
            ("input_cache_read", event.usage.input_cache_read_tokens),
            ("input_cache_write", event.usage.input_cache_write_tokens),
            ("output", event.usage.output_tokens),
            ("reasoning_output", event.usage.reasoning_output_tokens),
        ):
            if tokens is not None:
                self.tokens.labels(protocol, provider, model, kind).inc(tokens)
        currency = bundle.cost.currency or "unknown"
        self.cost_observations.labels(currency, bundle.cost.status.value).inc()
        if bundle.cost.known_amount_nanos is not None:
            self.known_estimated_cost.labels(currency, provider, model).inc(
                bundle.cost.known_amount_nanos / 1_000_000_000
            )

    def initialize_health(self, targets: Mapping[str, ModelTarget]) -> None:
        """Initialize bounded one-hot gauges for configured failure domains."""

        providers: set[tuple[str, str]] = set()
        targets_by_provider: dict[str, list[tuple[str, str]]] = {}
        for target in targets.values():
            protocol = target.protocol.value
            providers.add((protocol, target.provider))
            targets_by_provider.setdefault(target.provider, []).append((protocol, target.alias))
            for state in HealthState:
                value = 1 if state is HealthState.HEALTHY else 0
                self.health_state.labels(protocol, target.provider, target.alias, state.value).set(value)
        for protocol, provider in providers:
            for state in HealthState:
                value = 1 if state is HealthState.HEALTHY else 0
                self.health_state.labels(protocol, provider, "all", state.value).set(value)
        self._targets_by_provider = {
            provider: tuple(entries) for provider, entries in targets_by_provider.items()
        }
        self._target_tiers = {
            alias: target.tier.value for alias, target in targets.items()
        }

    def record_health_snapshot(
        self,
        snapshot: AvailabilitySnapshot,
        targets: Mapping[str, ModelTarget],
    ) -> None:
        """Refresh composite target gauges from one immutable snapshot."""

        for alias, availability in snapshot.target_states.items():
            target = targets.get(alias)
            if target is None:
                continue
            for state in HealthState:
                value = 1 if state is availability.state else 0
                self.health_state.labels(
                    target.protocol.value,
                    target.provider,
                    alias,
                    state.value,
                ).set(value)

    def record_health_transition(self, transition: HealthTransition, protocol: str) -> None:
        """Record one bounded Provider or Model Target health transition."""

        target = transition.target_alias or "all"
        self.health_state.labels(
            protocol, transition.provider, target, transition.from_state.value
        ).set(0)
        self.health_state.labels(
            protocol, transition.provider, target, transition.to_state.value
        ).set(1)
        if transition.target_alias is None:
            for target_protocol, target_alias in self._targets_by_provider.get(
                transition.provider, ()
            ):
                self.health_state.labels(
                    target_protocol,
                    transition.provider,
                    target_alias,
                    transition.from_state.value,
                ).set(0)
                self.health_state.labels(
                    target_protocol,
                    transition.provider,
                    target_alias,
                    transition.to_state.value,
                ).set(1)
        if transition.to_state is HealthState.HALF_OPEN:
            self.health_probes.labels(protocol, transition.provider, target).inc()
        if transition.to_state is HealthState.COOLDOWN:
            failure_class = (
                transition.failure_class.value if transition.failure_class is not None else "none"
            )
            self.health_cooldown_transitions.labels(
                protocol, transition.provider, target, failure_class
            ).inc()
            if transition.cooldown_seconds is not None:
                self.health_cooldown_seconds.labels(
                    protocol, transition.provider, target
                ).observe(transition.cooldown_seconds)
            if transition.from_state is HealthState.HALF_OPEN:
                self.health_recoveries.labels(
                    protocol, transition.provider, target, "failure"
                ).inc()
        if (
            transition.to_state is HealthState.HEALTHY
            and transition.from_state is HealthState.HALF_OPEN
        ):
            self.health_recoveries.labels(
                protocol, transition.provider, target, "success"
            ).inc()

    def render(self) -> bytes:
        """Render the private registry in Prometheus text format."""

        return generate_latest(self.registry)
