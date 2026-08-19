"""Application coordinator for online context capture and planning."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from llm_router import __version__
from llm_router.domain import ExecutionPlan, RoutingRequest
from llm_router.errors import RouterError
from llm_router.evaluation.canary_models import CanaryAssignment
from llm_router.evaluation.models import RouteDecisionInput, RouterErrorSnapshot
from llm_router.evaluation.recorder import DecisionRecorderPort
from llm_router.evaluation.shadow import NoopShadowEvaluator, ShadowEvaluatorPort
from llm_router.health.port import HealthPort
from llm_router.observability.metrics import RouterMetrics
from llm_router.routing.canary import PolicySelectorPort
from llm_router.routing.context import RoutingContext
from llm_router.routing.policy import RoutingPolicy
from llm_router.routing.session import SessionStateStore


@dataclass(frozen=True, slots=True)
class RoutingInvocation:
    """Carry transient online identity around a sanitized routing request."""

    request_id: UUID
    task_id: UUID | None
    session_key: str | None
    received_at: datetime
    request: RoutingRequest


@dataclass(frozen=True, slots=True)
class RoutingResolution:
    """Return exactly one result from the startup-fixed selected policy."""

    plan: ExecutionPlan | None
    error: RouterError | None
    routing_policy_hash: str
    policy_version: str
    assignment: CanaryAssignment | None
    started_at: datetime
    duration_ms: float

    def __post_init__(self) -> None:
        """Require exactly one plan or expected routing error."""

        if (self.plan is None) == (self.error is None):
            raise ValueError("a routing resolution must contain exactly one result")


class RoutingCoordinator:
    """Build context, invoke the Kernel, and capture sanitized decisions."""

    def __init__(
        self,
        selector: PolicySelectorPort,
        sessions: SessionStateStore,
        health: HealthPort,
        recorder: DecisionRecorderPort,
        shadow: ShadowEvaluatorPort | None = None,
        metrics: RouterMetrics | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Bind online runtime ports without taking execution ownership."""

        self._selector = selector
        self._sessions = sessions
        self._health = health
        self._recorder = recorder
        self._shadow = shadow or NoopShadowEvaluator()
        self._metrics = metrics
        self._clock = clock

    def resolve(self, invocation: RoutingInvocation) -> RoutingResolution:
        """Select once, snapshot once, and resolve through exactly one Kernel."""

        now = self._clock().astimezone(UTC)
        started = time.monotonic()
        selection = self._selector.select(invocation)
        policy = selection.kernel.policy
        context = RoutingContext(
            session=self._sessions.routing_snapshot(invocation.session_key),
            availability=self._health.snapshot(now),
        )
        try:
            plan = selection.kernel.plan(invocation.request, context)
        except RouterError as error:
            captured = self._capture(
                invocation,
                context,
                policy,
                selection.assignment,
                None,
                RouterErrorSnapshot.from_error(error),
                now,
            )
            self._record_metrics(selection.assignment, "error", captured)
            return RoutingResolution(
                None,
                error,
                policy.routing_policy_hash,
                policy.effective_policy_version,
                selection.assignment,
                now,
                (time.monotonic() - started) * 1000,
            )
        except Exception:
            self._record_metrics(selection.assignment, "internal_error", False)
            role = selection.assignment.role.value if selection.assignment is not None else "control"
            logging.getLogger("llm_router.routing").exception(
                "selected policy routing failed unexpectedly",
                extra={
                    "event": "routing_resolution_failed",
                    "policy_role": role,
                    "policy_hash": policy.routing_policy_hash,
                },
            )
            raise
        captured = self._capture(
            invocation, context, policy, selection.assignment, plan, None, now
        )
        self._record_metrics(selection.assignment, "plan", captured)
        return RoutingResolution(
            plan,
            None,
            policy.routing_policy_hash,
            policy.effective_policy_version,
            selection.assignment,
            now,
            (time.monotonic() - started) * 1000,
        )

    def _capture(
        self,
        invocation: RoutingInvocation,
        context: RoutingContext,
        policy: RoutingPolicy,
        assignment: CanaryAssignment | None,
        plan: ExecutionPlan | None,
        error: RouterErrorSnapshot | None,
        recorded_at: datetime,
    ) -> bool:
        """Construct one bounded record and delegate best-effort delivery."""

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
            canary_assignment=assignment,
        )
        captured = self._recorder.record(decision)
        try:
            self._shadow.submit(decision)
        except Exception:
            logging.getLogger("llm_router.routing").exception(
                "shadow submission failed",
                extra={"event": "shadow_submission_failed", "request_id": str(decision.request_id)},
            )
        return captured

    def _record_metrics(
        self,
        assignment: CanaryAssignment | None,
        result: str,
        captured: bool,
    ) -> None:
        """Record bounded Canary route facts without affecting requests."""

        if self._metrics is not None:
            self._metrics.record_canary_resolution(assignment, result, captured)
