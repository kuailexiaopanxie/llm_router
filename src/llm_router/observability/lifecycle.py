"""First-terminal-wins request observation lifecycle."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from llm_router.domain import Protocol
from llm_router.observability.models import (
    EndpointKind,
    ExecutionObservation,
    RequestStatus,
    RouteObservation,
    RoutingObservation,
    TerminalStage,
    TraceContext,
    UsageBreakdown,
)
from llm_router.observability.port import ObservationPort

if TYPE_CHECKING:
    from llm_router.observability.metrics import RouterMetrics


class RequestObservation:
    """Collect bounded request facts and emit the first terminal state once."""

    def __init__(
        self,
        sink: ObservationPort,
        request_id: UUID,
        trace: TraceContext,
        received_at: datetime,
        endpoint_kind: EndpointKind,
        metrics: RouterMetrics | None = None,
        on_close: Callable[[UUID], None] | None = None,
    ) -> None:
        """Create one request-scoped lifecycle before authentication."""

        self._sink = sink
        self._request_id = request_id
        self._trace = trace
        self._received_at = received_at.astimezone(UTC)
        self._endpoint_kind = endpoint_kind
        self._metrics = metrics
        self._on_close = on_close
        self._metric_protocol = "unknown"
        self._lock = threading.Lock()
        self._closed = False
        self._task_id: UUID | None = None
        self._protocol: Protocol | None = None
        self._profile: str | None = None
        self._stream = False
        self._active_stream = False
        self._routing: RoutingObservation | None = None
        self._execution: ExecutionObservation | None = None
        self._usage = UsageBreakdown.not_applicable()
        self._stage = TerminalStage.AUTHENTICATION
        if metrics is not None:
            metrics.inflight_requests.labels(self._metric_protocol).inc()

    @property
    def trace_context(self) -> TraceContext:
        """Expose normalized trace identity for response propagation."""

        return self._trace

    def request_facts(
        self,
        protocol: Protocol,
        profile: str | None,
        stream: bool,
        task_id: UUID | None,
    ) -> None:
        """Record sanitized request classification after validation."""

        self._protocol = protocol
        self._profile = profile
        self._stream = stream
        self._task_id = task_id
        self._stage = TerminalStage.ROUTING
        if self._metrics is not None and self._metric_protocol == "unknown":
            self._metrics.inflight_requests.labels("unknown").dec()
            self._metric_protocol = protocol.value
            self._metrics.inflight_requests.labels(self._metric_protocol).inc()

    def routed(self, facts: RoutingObservation) -> None:
        """Record actual selected policy and routing timing once available."""

        self._routing = facts
        self._stage = TerminalStage.ROUTING

    def execution_started(self, committed: bool) -> None:
        """Track execution stage for bounded shutdown abandonment."""

        if committed and self._stream and not self._active_stream and self._metrics is not None:
            self._active_stream = True
            self._metrics.active_streams.labels(self._metric_protocol).inc()
        self._stage = (
            TerminalStage.EXECUTION_POST_COMMIT
            if committed
            else TerminalStage.EXECUTION_PRE_COMMIT
        )

    def executing(self, facts: ExecutionObservation, usage: UsageBreakdown) -> None:
        """Record actual execution and normalized terminal usage."""

        self._execution = facts
        self._usage = usage
        self._stage = (
            TerminalStage.EXECUTION_POST_COMMIT
            if facts.committed and self._stream
            else TerminalStage.EXECUTION_PRE_COMMIT
        )

    def abandon(self) -> bool:
        """Finish an active request as abandoned during bounded shutdown."""

        return self.finish(RequestStatus.ABANDONED, self._stage, "router_shutdown_abandoned")

    def finish(
        self,
        status: RequestStatus,
        stage: TerminalStage,
        error_code: str | None = None,
        completed_at: datetime | None = None,
    ) -> bool:
        """Emit exactly the first terminal observation."""

        with self._lock:
            if self._closed:
                if self._metrics is not None:
                    self._metrics.duplicate_terminal.inc()
                return False
            self._closed = True
        event = RouteObservation(
            request_id=self._request_id,
            task_id=self._task_id,
            trace_context=self._trace,
            received_at=self._received_at,
            completed_at=(completed_at or datetime.now(UTC)).astimezone(UTC),
            endpoint_kind=self._endpoint_kind,
            protocol=self._protocol,
            profile=self._profile,
            stream=self._stream,
            terminal_stage=stage,
            status=status,
            routing=self._routing,
            execution=self._execution,
            usage=self._usage,
            error_code=error_code,
        )
        try:
            self._sink.record(event)
        except Exception:  # noqa: BLE001 - observations must never alter request handling.
            logging.getLogger("llm_router.observability").error(
                "Terminal observation delivery failed",
                extra={"event": "observation_delivery_failed"},
            )
        finally:
            if self._metrics is not None:
                self._metrics.inflight_requests.labels(self._metric_protocol).dec()
                if self._active_stream:
                    self._metrics.active_streams.labels(self._metric_protocol).dec()
            if self._on_close is not None:
                self._on_close(self._request_id)
        return True


class ActiveObservationRegistry:
    """Own active request lifecycles for bounded shutdown abandonment."""

    def __init__(self, sink: ObservationPort, metrics: RouterMetrics) -> None:
        """Bind one observation sink and live metrics registry."""

        self._sink = sink
        self._metrics = metrics
        self._lock = threading.Lock()
        self._active: dict[UUID, RequestObservation] = {}

    def open(
        self,
        request_id: UUID,
        trace: TraceContext,
        received_at: datetime,
        endpoint_kind: EndpointKind,
    ) -> RequestObservation:
        """Create and register one request lifecycle before authentication."""

        lifecycle = RequestObservation(
            self._sink,
            request_id,
            trace,
            received_at,
            endpoint_kind,
            self._metrics,
            self._remove,
        )
        with self._lock:
            self._active[request_id] = lifecycle
        return lifecycle

    def _remove(self, request_id: UUID) -> None:
        """Remove one terminal lifecycle from the active set."""

        with self._lock:
            self._active.pop(request_id, None)

    def abandon_all(self) -> int:
        """Mark every remaining lifecycle abandoned and return accepted count."""

        with self._lock:
            active = tuple(self._active.values())
        return sum(lifecycle.abandon() for lifecycle in active)
