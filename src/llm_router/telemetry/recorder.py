"""Non-blocking bounded telemetry recorder."""

from __future__ import annotations

import asyncio
import logging

from llm_router.domain import RouteEvent
from llm_router.telemetry.metrics import RouterMetrics
from llm_router.telemetry.port import EventStorePort


class TelemetryRecorder:
    """Write sanitized events asynchronously without blocking request completion."""

    def __init__(self, store: EventStorePort, metrics: RouterMetrics, capacity: int) -> None:
        self._store = store
        self._metrics = metrics
        self._queue: asyncio.Queue[RouteEvent] = asyncio.Queue(maxsize=capacity)
        self._worker: asyncio.Task[None] | None = None
        self._stopping = False
        self._logger = logging.getLogger("llm_router.telemetry")

    async def start(self) -> None:
        """Start the event store and bounded writer task."""

        await self._store.start()
        self._worker = asyncio.create_task(self._run(), name="llm-router-telemetry")

    async def _run(self) -> None:
        """Drain queued events and isolate persistence failures from requests."""

        while not self._stopping or not self._queue.empty():
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue
            try:
                await self._store.append(event)
                self._metrics.record(event)
            except Exception:
                self._logger.exception("telemetry write failed", extra={"event": "telemetry_write_failed"})
            finally:
                self._queue.task_done()

    def record(self, event: RouteEvent) -> None:
        """Queue an event or increment the drop counter when capacity is exhausted."""

        if self._stopping:
            self._metrics.telemetry_dropped.inc()
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._metrics.telemetry_dropped.inc()

    async def close(self, grace_seconds: float = 5) -> None:
        """Drain telemetry for a bounded grace period before closing SQLite."""

        self._stopping = True
        if self._worker is not None:
            try:
                await asyncio.wait_for(self._queue.join(), timeout=grace_seconds)
            except asyncio.TimeoutError:
                self._logger.warning("telemetry drain deadline exceeded", extra={"event": "telemetry_drain_timeout"})
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        await self._store.close()
