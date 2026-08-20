"""Low-cardinality Prometheus metrics for dashboard reads."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram


class DashboardMetrics:
    """Own metrics whose labels are fixed dashboard enums."""

    def __init__(self, registry: CollectorRegistry) -> None:
        """Register dashboard collectors in the router registry."""

        self.queries = Counter("llm_router_dashboard_queries_total", "Dashboard queries", ["view", "status"], registry=registry)
        self.duration = Histogram("llm_router_dashboard_query_duration_seconds", "Dashboard query duration", ["view"], registry=registry)
        self.active = Gauge("llm_router_dashboard_active_queries", "Active dashboard queries", registry=registry)
        self.auth = Counter("llm_router_dashboard_auth_total", "Dashboard authentication", ["status"], registry=registry)
