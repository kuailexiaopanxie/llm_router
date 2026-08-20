"""Sanitized current-process snapshot adapter for the dashboard."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from llm_router.dashboard.models import DashboardRuntimeSnapshot, DashboardTargetHealth


class DashboardRuntimeSource(Protocol):
    """Expose current bounded runtime facts without mutation access."""

    def snapshot(self) -> DashboardRuntimeSnapshot:
        """Return one immutable sanitized process snapshot."""


class RuntimeSnapshotSource:
    """Project application runtime into a secret-free dashboard value."""

    def __init__(self, runtime: object, started_at: datetime) -> None:
        """Bind the runtime object behind a narrow read-only projection."""

        self._runtime = runtime
        self._started_at = started_at.astimezone(UTC)

    def snapshot(self) -> DashboardRuntimeSnapshot:
        """Read health, canary, and observation facts once."""

        runtime = self._runtime
        now = datetime.now(UTC)
        config = runtime.config  # type: ignore[attr-defined]
        health = runtime.health.snapshot(now)  # type: ignore[attr-defined]
        target_config = config.model_targets()
        targets = tuple(
            DashboardTargetHealth(
                alias=alias,
                provider=target_config[alias].provider,
                state=availability.state.value,
                eligible=availability.eligible,
                retry_at=availability.retry_at,
            )
            for alias, availability in sorted(health.target_states.items())
        )
        observation = runtime.observations.runtime_state.snapshot()  # type: ignore[attr-defined]
        canary = runtime.canary_state  # type: ignore[attr-defined]
        return DashboardRuntimeSnapshot(
            observed_at=now, ready=bool(runtime.ready), started_at=self._started_at,  # type: ignore[attr-defined]
            router_version="0.8.0", capture_enabled=config.observability.capture_enabled,
            trace_enabled=config.observability.tracing.enabled,
            local_trace_store=config.observability.tracing.local_store,
            observation_queue_depth=observation.queue_depth,
            observation_queue_capacity=observation.queue_capacity,
            observation_dropped_since_start=observation.dropped_since_start,
            sqlite_failures_since_start=observation.sqlite_failures_since_start,
            canary_enabled=config.canary.enabled, canary_active=canary.active,
            canary_reason=canary.reason.value if canary.reason else None,
            health_revision=health.revision, targets=targets,
        )
