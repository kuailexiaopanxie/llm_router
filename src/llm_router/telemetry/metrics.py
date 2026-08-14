"""Bounded-label Prometheus metrics for router operations."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

from llm_router.domain import RouteEvent


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

    def record(self, event: RouteEvent) -> None:
        """Record one completed route event using bounded configured labels."""

        self.requests.labels(event.profile, event.status).inc()
        self.routes.labels(event.profile, event.final_model.split(":", 1)[0], event.final_model).inc()
        self.routing_latency.observe(event.total_latency_ms)
        if event.time_to_first_event_ms is not None:
            self.first_event_latency.observe(event.time_to_first_event_ms)
        for attempt in event.attempts:
            self.attempts.labels(attempt.provider, attempt.model, attempt.status).inc()

    def render(self) -> bytes:
        """Render the private registry in Prometheus text format."""

        return generate_latest(self.registry)

