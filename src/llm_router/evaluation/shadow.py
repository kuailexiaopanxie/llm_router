"""Non-blocking online shadow policy evaluation."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Protocol

from llm_router.evaluation.canary_models import PolicyRole
from llm_router.evaluation.models import (
    ReplayCase,
    ReplayResult,
    RouteDecisionInput,
    RoutingPolicySnapshot,
    ShadowDecision,
    ShadowReason,
    ShadowStatus,
)
from llm_router.evaluation.port import ShadowStorePort
from llm_router.evaluation.replay import ReplayEngine
from llm_router.telemetry.metrics import RouterMetrics


class ShadowEvaluatorPort(Protocol):
    """Accept one immutable actual decision without blocking the request."""

    def submit(self, decision: RouteDecisionInput) -> None:
        """Apply admission rules and enqueue one shadow evaluation best-effort."""


class NoopShadowEvaluator:
    """Discard shadow decisions when the feature is disabled or unavailable."""

    def __init__(self, metrics: RouterMetrics | None = None) -> None:
        """Bind optional metrics for disabled admissions."""

        self._metrics = metrics

    def submit(self, decision: RouteDecisionInput) -> None:
        """Accept and intentionally discard one actual decision."""

        if self._metrics is not None:
            self._metrics.shadow_admission.labels("disabled").inc()


class UnavailableShadowEvaluator:
    """Expose candidate bootstrap failure without affecting actual routing."""

    def __init__(self, metrics: RouterMetrics | None = None) -> None:
        """Bind optional bounded metrics for unavailable admissions."""

        self._metrics = metrics

    def submit(self, decision: RouteDecisionInput) -> None:
        """Record that an otherwise eligible decision had no candidate."""

        if self._metrics is not None:
            self._metrics.shadow_admission.labels("unavailable").inc()


class ShadowEvaluator:
    """Sample, replay, and persist one fixed candidate policy asynchronously."""

    def __init__(
        self,
        engine: ReplayEngine,
        current_policy: RoutingPolicySnapshot,
        store: ShadowStorePort,
        sample_rate: float,
        protocols: frozenset[str],
        profiles: frozenset[str],
        queue_capacity: int,
        evaluation_timeout_ms: int,
        candidate_available: bool = True,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        metrics: RouterMetrics | None = None,
    ) -> None:
        """Bind one immutable candidate and bounded worker configuration."""

        self._engine = engine
        self._current_policy = current_policy
        self._store = store
        self._sample_rate = sample_rate
        self._protocols = protocols
        self._profiles = profiles
        self._queue: asyncio.Queue[RouteDecisionInput] = asyncio.Queue(maxsize=queue_capacity)
        self._timeout_seconds = evaluation_timeout_ms / 1000
        self._candidate_available = candidate_available
        self._clock = clock
        self._metrics = metrics
        self._worker: asyncio.Task[None] | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="llm-router-shadow")
        self._stopping = False
        self._logger = logging.getLogger("llm_router.evaluation.shadow")

    @property
    def candidate_policy_hash(self) -> str:
        """Return the fixed candidate policy identity."""

        return self._engine.candidate_policy_hash

    async def start(self) -> None:
        """Start one bounded shadow worker."""

        if self._worker is None:
            self._stopping = False
            self._worker = asyncio.create_task(self._run(), name="llm-router-shadow-evaluator")

    def submit(self, decision: RouteDecisionInput) -> None:
        """Admit one decision without waiting or performing persistence I/O."""

        try:
            if self._stopping or self._worker is None:
                self._admission("disabled")
                return
            if (
                decision.canary_assignment is not None
                and decision.canary_assignment.role is PolicyRole.CANARY
            ):
                self._admission("actual_is_candidate")
                return
            if not self._candidate_available:
                self._admission("unavailable")
                return
            if self._protocols and decision.request.protocol.value not in self._protocols:
                self._admission("filtered")
                return
            profile = (
                decision.actual_plan.profile
                if decision.actual_plan is not None
                else decision.request.requested_profile
            ) or self._engine.candidate_policy.default_profile
            if self._profiles and profile not in self._profiles:
                self._admission("filtered")
                return
            if not self._sampled(decision.request_id):
                self._admission("unsampled")
                return
            self._queue.put_nowait(decision)
            self._admission("enqueued")
            if self._metrics is not None:
                self._metrics.shadow_queue_depth.set(self._queue.qsize())
        except asyncio.QueueFull:
            self._admission("queue_dropped")
        except Exception:
            self._admission("unavailable")
            self._logger.exception("shadow admission failed", extra={"event": "shadow_admission_failed"})

    def _sampled(self, request_id: object) -> bool:
        """Apply deterministic request and candidate hash sampling."""

        digest = hashlib.sha256(f"{request_id}:{self.candidate_policy_hash}".encode()).digest()
        bucket = int.from_bytes(digest[:8], "big") % 10_000
        threshold = round(self._sample_rate * 10_000)
        return bucket < threshold

    def _admission(self, status: str) -> None:
        """Increment one fixed-cardinality admission counter."""

        if self._metrics is not None:
            self._metrics.shadow_admission.labels(status).inc()

    async def _run(self) -> None:
        """Drain admitted decisions and isolate all candidate-side failures."""

        while not self._stopping or not self._queue.empty():
            try:
                decision = await asyncio.wait_for(self._queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue
            try:
                await self._evaluate(decision)
            finally:
                self._queue.task_done()
                if self._metrics is not None:
                    self._metrics.shadow_queue_depth.set(self._queue.qsize())

    async def _evaluate(self, decision: RouteDecisionInput) -> None:
        """Run one replay with a bounded deadline and persist its comparison."""

        started = self._clock().astimezone(UTC)
        try:
            result = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    self._executor,
                    self._engine.replay,
                    ReplayCase(decision, self._current_policy, ()),
                ),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError:
            self._evaluation_metric("timeout")
            return
        except Exception:
            self._evaluation_metric("failed")
            self._logger.exception("shadow evaluation failed", extra={"event": "shadow_evaluation_failed"})
            await self._persist(self._failed_decision(decision, self._clock().astimezone(UTC)))
            return
        self._evaluation_metric(
            "evaluated" if result.status is not None and result.status.value == "replayed" else "non_replayable"
        )
        if self._metrics is not None:
            self._metrics.shadow_evaluation_duration.observe(
                max(0.0, (self._clock().astimezone(UTC) - started).total_seconds())
            )
        shadow = self._to_decision(decision, result, self._clock().astimezone(UTC))
        await self._persist(shadow)

    async def _persist(self, shadow: ShadowDecision) -> None:
        """Persist one comparison while isolating store and codec failures."""

        try:
            status = await self._store.append_shadow(shadow)
            if self._metrics is not None:
                self._metrics.shadow_persistence.labels(status).inc()
            if shadow.change is not None and self._metrics is not None:
                self._metrics.shadow_change.labels(shadow.change.value).inc()
        except Exception:
            if self._metrics is not None:
                self._metrics.shadow_persistence.labels("failed").inc()
            self._logger.exception("shadow persistence failed", extra={"event": "shadow_persistence_failed"})

    def _evaluation_metric(self, status: str) -> None:
        """Record one bounded evaluation status."""

        if self._metrics is not None:
            self._metrics.shadow_evaluation.labels(status).inc()

    def _to_decision(
        self, decision: RouteDecisionInput, result: ReplayResult, evaluated_at: datetime
    ) -> ShadowDecision:
        """Convert a ReplayResult into the bounded ShadowDecision domain."""

        reason = self._reason(result.reason)
        status = (
            ShadowStatus.EVALUATED
            if result.status.value == "replayed"
            else ShadowStatus.NON_REPLAYABLE
        )
        return ShadowDecision(
            request_id=decision.request_id,
            recorded_at=decision.recorded_at,
            evaluated_at=evaluated_at,
            protocol=decision.request.protocol,
            requested_profile=decision.request.requested_profile or self._engine.candidate_policy.default_profile,
            actual_policy_hash=decision.routing_policy_hash,
            candidate_policy_hash=result.candidate_policy_hash,
            candidate_algorithm_version=self._engine.candidate_policy.routing_algorithm_version,
            actual_plan=decision.actual_plan,
            actual_error=decision.actual_error,
            candidate_plan=result.candidate_plan,
            candidate_error=result.candidate_error,
            status=status,
            change=result.change if status is ShadowStatus.EVALUATED else None,
            reason=reason if status is ShadowStatus.NON_REPLAYABLE else None,
        )

    def _failed_decision(self, decision: RouteDecisionInput, evaluated_at: datetime) -> ShadowDecision:
        """Create a bounded persisted result for a replay implementation failure."""

        return ShadowDecision(
            request_id=decision.request_id,
            recorded_at=decision.recorded_at,
            evaluated_at=evaluated_at,
            protocol=decision.request.protocol,
            requested_profile=decision.request.requested_profile
            or self._engine.candidate_policy.default_profile,
            actual_policy_hash=decision.routing_policy_hash,
            candidate_policy_hash=self.candidate_policy_hash,
            candidate_algorithm_version=self._engine.candidate_policy.routing_algorithm_version,
            actual_plan=decision.actual_plan,
            actual_error=decision.actual_error,
            candidate_plan=None,
            candidate_error=None,
            status=ShadowStatus.EVALUATION_FAILED,
            reason=ShadowReason.EVALUATION_EXCEPTION,
        )

    @staticmethod
    def _reason(value: str | None) -> ShadowReason:
        """Map replay compatibility reasons to the public shadow vocabulary."""

        if value is None:
            return ShadowReason.CONTEXT_INVALID
        mapping = {
            "availability_identity_missing": ShadowReason.AVAILABILITY_IDENTITY_MISSING,
            "replay_algorithm_incompatible": ShadowReason.ALGORITHM_INCOMPATIBLE,
            "replay_schema_incompatible": ShadowReason.SCHEMA_INCOMPATIBLE,
            "replay_policy_missing": ShadowReason.POLICY_MISSING,
            "replay_policy_invalid": ShadowReason.POLICY_INVALID,
            "historical_reproduction_mismatch": ShadowReason.HISTORICAL_REPRODUCTION_MISMATCH,
        }
        return mapping.get(value, ShadowReason.CONTEXT_INVALID)

    async def close(self, grace_seconds: float = 5) -> None:
        """Stop admission and drain the bounded queue within a deadline."""

        self._stopping = True
        if self._worker is None:
            return
        try:
            await asyncio.wait_for(self._queue.join(), timeout=grace_seconds)
        except asyncio.TimeoutError:
            if self._metrics is not None:
                self._metrics.shadow_evaluation.labels("timeout").inc()
            self._logger.warning("shadow drain deadline exceeded", extra={"event": "shadow_drain_timeout"})
        self._worker.cancel()
        await asyncio.gather(self._worker, return_exceptions=True)
        self._worker = None
        self._executor.shutdown(wait=False, cancel_futures=True)
