"""Application coordinator for online context capture and planning."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from llm_router import __version__
from llm_router.domain import ExecutionPlan, RoutingRequest
from llm_router.errors import RouterError
from llm_router.evaluation.models import RouteDecisionInput, RouterErrorSnapshot
from llm_router.evaluation.recorder import DecisionRecorderPort
from llm_router.evaluation.shadow import NoopShadowEvaluator, ShadowEvaluatorPort
from llm_router.health.port import HealthPort
from llm_router.routing.context import RoutingContext
from llm_router.routing.kernel import RoutingKernel
from llm_router.routing.session import SessionStateStore


@dataclass(frozen=True, slots=True)
class RoutingInvocation:
    """Carry transient online identity around a sanitized routing request."""

    request_id: UUID
    task_id: UUID | None
    session_key: str | None
    received_at: datetime
    request: RoutingRequest


class RoutingCoordinator:
    """Build context, invoke the Kernel, and capture sanitized decisions."""

    def __init__(
        self,
        kernel: RoutingKernel,
        sessions: SessionStateStore,
        health: HealthPort,
        recorder: DecisionRecorderPort,
        shadow: ShadowEvaluatorPort | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Bind online runtime ports without taking execution ownership."""

        self._kernel = kernel
        self._sessions = sessions
        self._health = health
        self._recorder = recorder
        self._shadow = shadow or NoopShadowEvaluator()
        self._clock = clock

    def plan(self, invocation: RoutingInvocation) -> ExecutionPlan:
        """Snapshot runtime facts, plan, and queue one sanitized decision."""

        now = self._clock().astimezone(UTC)
        context = RoutingContext(
            session=self._sessions.routing_snapshot(invocation.session_key),
            availability=self._health.snapshot(now),
        )
        try:
            plan = self._kernel.plan(invocation.request, context)
        except RouterError as error:
            self._capture(invocation, context, None, RouterErrorSnapshot.from_error(error), now)
            raise
        self._capture(invocation, context, plan, None, now)
        return plan

    def _capture(
        self,
        invocation: RoutingInvocation,
        context: RoutingContext,
        plan: ExecutionPlan | None,
        error: RouterErrorSnapshot | None,
        recorded_at: datetime,
    ) -> None:
        """Construct one bounded record and delegate best-effort delivery."""

        policy = self._kernel.policy
        decision = RouteDecisionInput(
            request_id=invocation.request_id,
            task_id=invocation.task_id,
            recorded_at=recorded_at,
            router_version=__version__,
            routing_algorithm_version=policy.routing_algorithm_version,
            routing_policy_hash=policy.routing_policy_hash,
            request=invocation.request,
            session=context.session,
            availability=context.availability,
            actual_plan=plan,
            actual_error=error,
        )
        self._recorder.record(decision)
        try:
            self._shadow.submit(decision)
        except Exception:
            logging.getLogger("llm_router.routing").exception(
                "shadow submission failed",
                extra={"event": "shadow_submission_failed", "request_id": str(decision.request_id)},
            )
