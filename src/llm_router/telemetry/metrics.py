"""Bounded-label Prometheus metrics for router operations."""

from __future__ import annotations

from collections.abc import Mapping

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

from llm_router.domain import ModelTarget, RouteEvent
from llm_router.health.models import AvailabilitySnapshot, HealthState, HealthTransition


class RouterMetrics:
    """Keep metrics labels limited to configured route dimensions."""

    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "llm_router_requests_total", "Requests handled", ["profile", "status"], registry=self.registry
        )
        self.routes = Counter(
            "llm_router_routes_total", "Requests by final model", ["profile", "tier", "model"], registry=self.registry
        )
        self.attempts = Counter(
            "llm_router_attempts_total", "Provider attempts", ["provider", "model", "status"], registry=self.registry
        )
        self.routing_latency = Histogram(
            "llm_router_total_latency_ms", "End-to-end latency", registry=self.registry
        )
        self.first_event_latency = Histogram(
            "llm_router_first_event_latency_ms", "Time to first stream event", registry=self.registry
        )
        self.active_streams = Gauge("llm_router_active_streams", "Active SSE streams", registry=self.registry)
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
        self._targets_by_provider: dict[str, tuple[tuple[str, str], ...]] = {}

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

    def record(self, event: RouteEvent) -> None:
        """Record one completed route event using bounded configured labels."""

        self.requests.labels(event.profile, event.status).inc()
        self.routes.labels(event.profile, event.final_model.split(":", 1)[0], event.final_model).inc()
        self.routing_latency.observe(event.total_latency_ms)
        if event.time_to_first_event_ms is not None:
            self.first_event_latency.observe(event.time_to_first_event_ms)
        for attempt in event.attempts:
            self.attempts.labels(attempt.provider, attempt.model, attempt.status).inc()
            if attempt.status == "health_skipped":
                self.health_skipped.labels(
                    event.protocol, attempt.provider, attempt.model
                ).inc()
        if event.error_code == "router_no_available_target":
            self.no_available_target.labels(event.protocol, event.profile).inc()

    def render(self) -> bytes:
        """Render the private registry in Prometheus text format."""

        return generate_latest(self.registry)
