"""Bounded best-effort Decision Input recorder."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from llm_router.evaluation.codec import (
    MAX_DECISION_INPUT_BYTES,
    CodecError,
    decision_size_bytes,
)
from llm_router.evaluation.models import RouteDecisionInput
from llm_router.evaluation.port import DecisionStorePort
from llm_router.observability.metrics import RouterMetrics


class DecisionRecorderPort(Protocol):
    """Accept sanitized decision records without blocking requests."""

    def record(self, decision: RouteDecisionInput) -> bool:
        """Queue one record and report bounded admission status."""


class NoopDecisionRecorder:
    """Discard decisions when capture is disabled."""

    def record(self, decision: RouteDecisionInput) -> bool:
        """Discard one decision and report that capture is unavailable."""

        return False


class DecisionRecorder:
    """Drain a bounded queue into the evaluation store."""

    def __init__(
        self, store: DecisionStorePort, capacity: int, metrics: RouterMetrics | None = None
    ) -> None:
        """Create a bounded writer using the storage queue capacity."""

        self._store = store
        self._queue: asyncio.Queue[RouteDecisionInput] = asyncio.Queue(maxsize=capacity)
        self._worker: asyncio.Task[None] | None = None
        self._stopping = False
        self._logger = logging.getLogger("llm_router.evaluation")
        self._metrics = metrics

    async def start(self) -> None:
        """Start the best-effort decision writer."""

        self._worker = asyncio.create_task(self._run(), name="llm-router-decision-recorder")

    async def _run(self) -> None:
        """Drain queued decisions while isolating persistence failures."""

        while not self._stopping or not self._queue.empty():
            try:
                decision = await asyncio.wait_for(self._queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue
            try:
                await self._store.append_decision(decision)
                if self._metrics is not None:
                    self._metrics.decision_capture.labels("written").inc()
            except Exception:
                if self._metrics is not None:
                    self._metrics.decision_capture.labels("failed").inc()
                self._logger.exception(
                    "decision capture failed",
                    extra={"event": "decision_capture_failed", "request_id": str(decision.request_id)},
                )
            finally:
                self._queue.task_done()

    def record(self, decision: RouteDecisionInput) -> bool:
        """Queue one decision or safely report a bounded drop."""

        if self._stopping:
            return False
        try:
            if decision_size_bytes(decision) > MAX_DECISION_INPUT_BYTES:
                if self._metrics is not None:
                    self._metrics.decision_capture.labels("dropped").inc()
                return False
        except (CodecError, ValueError):
            if self._metrics is not None:
                self._metrics.decision_capture.labels("failed").inc()
            return False
        try:
            self._queue.put_nowait(decision)
            if self._metrics is not None:
                self._metrics.decision_capture.labels("queued").inc()
            return True
        except asyncio.QueueFull:
            if self._metrics is not None:
                self._metrics.decision_capture.labels("dropped").inc()
            self._logger.warning("decision capture queue is full", extra={"event": "decision_capture_dropped"})
            return False

    async def close(self, grace_seconds: float = 5) -> None:
        """Drain queued decisions within a bounded shutdown grace period."""

        self._stopping = True
        if self._worker is None:
            return
        try:
            await asyncio.wait_for(self._queue.join(), timeout=grace_seconds)
        except asyncio.TimeoutError:
            self._logger.warning(
                "decision capture drain deadline exceeded",
                extra={"event": "decision_capture_drain_timeout"},
            )
        self._worker.cancel()
        await asyncio.gather(self._worker, return_exceptions=True)
