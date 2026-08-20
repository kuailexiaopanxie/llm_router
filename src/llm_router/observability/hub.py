"""Fail-open enrichment and independent observation sink fan-out."""

from __future__ import annotations

import asyncio
import logging

from llm_router.observability.metrics import RouterMetrics
from llm_router.observability.models import (
    CostEstimate,
    CostStatus,
    ObservationBundle,
    RouteObservation,
)
from llm_router.observability.port import ObservationStorePort, TraceExporterPort
from llm_router.observability.pricing import CostCalculator
from llm_router.observability.runtime_state import ObservationRuntimeState
from llm_router.observability.tracing import TraceBuilder


class ObservationHub:
    """Enrich terminal facts and isolate metrics, SQLite, and OTLP sinks."""

    def __init__(
        self,
        store: ObservationStorePort,
        metrics: RouterMetrics,
        calculator: CostCalculator,
        traces: TraceBuilder,
        exporter: TraceExporterPort,
        capture_enabled: bool,
        queue_capacity: int,
        local_trace_store: bool = True,
        runtime_state: ObservationRuntimeState | None = None,
    ) -> None:
        """Bind immutable enrichers and a bounded durable queue."""

        self._store = store
        self._metrics = metrics
        self._calculator = calculator
        self._traces = traces
        self._exporter = exporter
        self._capture_enabled = capture_enabled
        self._local_trace_store = local_trace_store
        self._queue: asyncio.Queue[ObservationBundle] = asyncio.Queue(maxsize=queue_capacity)
        self._worker: asyncio.Task[None] | None = None
        self._stopping = False
        self._logger = logging.getLogger("llm_router.observability")
        self.runtime_state = runtime_state or ObservationRuntimeState(queue_capacity)

    async def start(self) -> None:
        """Start durable and exporter workers independently."""

        await self._exporter.start()
        if self._capture_enabled:
            self._worker = asyncio.create_task(self._run(), name="llm-router-observation-writer")

    def record(self, event: RouteObservation) -> None:
        """Enrich and fan out one terminal fact without raising to Gateway."""

        try:
            cost = self._calculator.estimate(event)
        except Exception:  # noqa: BLE001 - explicit fail-open enrichment boundary.
            self._metrics.observation_sink_failures.labels("pricing", "invalid_fact").inc()
            cost = CostEstimate(CostStatus.USAGE_MISSING, None, None, None)
        try:
            spans = self._traces.build(event, cost)
        except Exception:  # noqa: BLE001 - trace enrichment is independently optional.
            self._metrics.observation_sink_failures.labels("tracing", "invalid_fact").inc()
            spans = ()
        bundle = ObservationBundle(event, cost, spans if self._local_trace_store else ())
        try:
            self._metrics.record_observation(bundle)
        except Exception:  # noqa: BLE001 - metrics must not affect durable capture.
            self._metrics.observation_sink_failures.labels("metrics", "update_failed").inc()
        try:
            if spans:
                self._exporter.record(spans)
        except Exception:  # noqa: BLE001 - exporter has an independent queue.
            self._metrics.observation_sink_failures.labels("otlp", "queue_failed").inc()
            self.runtime_state.otlp_failed()
        if not self._capture_enabled or self._stopping:
            return
        try:
            self._queue.put_nowait(bundle)
            self._metrics.observation_queue_depth.set(self._queue.qsize())
            self.runtime_state.queue(self._queue.qsize())
        except asyncio.QueueFull:
            self._metrics.observation_dropped.labels("queue_full").inc()
            self.runtime_state.dropped()
            self._logger.warning("Observation queue is full", extra={"event": "observation_dropped"})

    async def _run(self) -> None:
        """Drain durable bundles while isolating SQLite failures."""

        while not self._stopping or not self._queue.empty():
            try:
                bundle = await asyncio.wait_for(self._queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue
            try:
                result = await self._store.append(bundle)
                if result == "duplicate":
                    self._metrics.observation_dropped.labels("duplicate").inc()
            except Exception:  # noqa: BLE001 - durable sink is best-effort after startup.
                self._metrics.observation_sink_failures.labels("sqlite", "write_failed").inc()
                self.runtime_state.sqlite_failed()
                self._logger.error(
                    "Observation store write failed", extra={"event": "observation_store_failed"}
                )
            finally:
                self._queue.task_done()
                self._metrics.observation_queue_depth.set(self._queue.qsize())
                self.runtime_state.queue(self._queue.qsize())

    async def close(self, grace_seconds: float = 5) -> None:
        """Stop intake and drain each sink within bounded grace."""

        self._stopping = True
        if self._worker is not None:
            try:
                await asyncio.wait_for(self._queue.join(), timeout=grace_seconds)
            except asyncio.TimeoutError:
                dropped = self._queue.qsize()
                if dropped:
                    self._metrics.observation_dropped.labels("shutdown_timeout").inc(dropped)
                    self.runtime_state.dropped(dropped)
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        await self._exporter.close()
