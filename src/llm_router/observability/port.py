"""Stable ports for terminal observations, persistence, and trace export."""

from __future__ import annotations

from typing import Protocol

from llm_router.observability.models import (
    ObservationBundle,
    RouteObservation,
    TraceSpan,
)


class ObservationPort(Protocol):
    """Accept one immutable terminal observation without affecting execution."""

    def record(self, event: RouteObservation) -> None:
        """Update live metrics and enqueue durable observation best-effort."""


class ObservationStorePort(Protocol):
    """Persist immutable observation bundles atomically."""

    async def start(self) -> None:
        """Initialize the durable sink."""

    async def append(self, bundle: ObservationBundle) -> str:
        """Persist a bundle and return written or duplicate."""

    async def close(self) -> None:
        """Close the durable sink."""


class TraceExporterPort(Protocol):
    """Accept bounded spans without blocking request completion."""

    async def start(self) -> None:
        """Start exporter workers."""

    def record(self, spans: tuple[TraceSpan, ...]) -> None:
        """Enqueue spans without blocking."""

    async def close(self) -> None:
        """Drain and stop exporter workers."""


class NoopObservationHub:
    """Discard terminal observations when every sink is disabled."""

    async def start(self) -> None:
        """Start no workers."""

    def record(self, event: RouteObservation) -> None:
        """Accept and discard one immutable observation."""

    async def close(self) -> None:
        """Close no resources."""


class NoopTraceExporter:
    """Discard sampled spans when OTLP is disabled."""

    async def start(self) -> None:
        """Start no worker."""

    def record(self, spans: tuple[TraceSpan, ...]) -> None:
        """Discard one bounded span batch."""

    async def close(self) -> None:
        """Close no resources."""
