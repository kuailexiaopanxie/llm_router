"""Side-effect-free replay of historical routing decisions."""

from __future__ import annotations

from datetime import datetime
from types import MappingProxyType

from llm_router.domain import ExecutionPlan, ModelTarget, RoutingRequest
from llm_router.errors import RouterError
from llm_router.evaluation.codec import (
    CodecError,
    decode_policy_snapshot,
    encode_error,
    encode_plan,
)
from llm_router.evaluation.models import (
    ReplayCase,
    ReplayChange,
    ReplayMode,
    ReplayResult,
    ReplayStatus,
    RouterErrorSnapshot,
)
from llm_router.health.models import (
    AvailabilityReason,
    AvailabilitySnapshot,
    HealthState,
    TargetAvailability,
)
from llm_router.routing.context import RoutingContext
from llm_router.routing.kernel import RoutingKernel
from llm_router.routing.policy import RoutingPolicy


class ReplayFatalError(RuntimeError):
    """Report a fatal implementation or reproduction inconsistency."""


def _identity(target: ModelTarget) -> tuple[str, str, str, str]:
    """Return the operational identity used by historical availability."""

    return (target.alias, target.provider, target.upstream_model, target.protocol.value)


def _may_participate(policy: RoutingPolicy, request: RoutingRequest) -> tuple[ModelTarget, ...]:
    """Conservatively select targets that may enter one candidate decision."""

    profile = policy.profiles.get(request.requested_profile or policy.default_profile)
    if profile is None:
        return ()
    if not profile.automatic:
        chain = profile.targets.get(request.protocol)
        if chain is None:
            return ()
        return tuple(policy.targets[alias] for alias in (chain.primary, *chain.fallback))
    return tuple(
        target
        for target in policy.targets.values()
        if target.protocol is request.protocol
        and request.required_capabilities.issubset(target.capabilities)
        and request.estimated_input_tokens <= target.max_input_tokens
    )


def _all_healthy(policy: RoutingPolicy, observed_at: datetime) -> AvailabilitySnapshot:
    """Build deterministic healthy facts for every candidate target."""

    states = {
        alias: TargetAvailability(True, HealthState.HEALTHY, AvailabilityReason.HEALTHY)
        for alias in sorted(policy.targets)
    }
    return AvailabilitySnapshot(0, observed_at, MappingProxyType(states), None)


def _plan_change(actual: ExecutionPlan, candidate: ExecutionPlan) -> ReplayChange:
    """Classify execution-relevant plan differences."""

    if _identity(actual.primary) != _identity(candidate.primary):
        return ReplayChange.PRIMARY_CHANGED
    if (
        tuple(_identity(item) for item in actual.targets)
        != tuple(_identity(item) for item in candidate.targets)
        or actual.attempt_limit != candidate.attempt_limit
        or actual.timeouts != candidate.timeouts
    ):
        return ReplayChange.CHAIN_CHANGED
    return ReplayChange.UNCHANGED


def _result_change(
    actual_plan: ExecutionPlan | None,
    actual_error: RouterErrorSnapshot | None,
    candidate_plan: ExecutionPlan | None,
    candidate_error: RouterErrorSnapshot | None,
) -> ReplayChange:
    """Classify one normalized actual and candidate result pair."""

    if actual_plan is not None and candidate_error is not None:
        return ReplayChange.PLAN_TO_ERROR
    if actual_error is not None and candidate_plan is not None:
        return ReplayChange.ERROR_TO_PLAN
    if actual_plan is not None and candidate_plan is not None:
        return _plan_change(actual_plan, candidate_plan)
    assert actual_error is not None and candidate_error is not None
    return ReplayChange.UNCHANGED if encode_error(actual_error) == encode_error(candidate_error) else ReplayChange.ERROR_CHANGED


class ReplayEngine:
    """Compare candidate plans using captured facts and the production Kernel."""

    def __init__(self, candidate_policy: RoutingPolicy, mode: ReplayMode | str) -> None:
        """Construct one reusable candidate Kernel for a replay run."""

        self._candidate = candidate_policy
        self._mode = ReplayMode(mode)
        self._kernel = RoutingKernel(candidate_policy)

    @property
    def candidate_policy(self) -> RoutingPolicy:
        """Return the immutable candidate policy used by this engine."""

        return self._candidate

    @property
    def candidate_policy_hash(self) -> str:
        """Return the candidate policy hash used for sampling and storage."""

        return self._candidate.routing_policy_hash

    def replay(self, case: ReplayCase) -> ReplayResult:
        """Replay one case or return a bounded non-replayable reason."""

        decision = case.decision
        if decision.schema_version != 1:
            return self._non_replayable(case, "replay_schema_incompatible")
        if decision.routing_algorithm_version != self._candidate.routing_algorithm_version:
            return self._non_replayable(case, "replay_algorithm_incompatible")
        if case.historical_policy is None:
            return self._non_replayable(case, "replay_policy_missing")
        try:
            historical = decode_policy_snapshot(case.historical_policy)
        except CodecError:
            return self._non_replayable(case, "replay_policy_invalid")
        if historical.routing_policy_hash != decision.routing_policy_hash:
            return self._non_replayable(case, "replay_policy_missing")
        if self._mode is ReplayMode.HISTORICAL:
            historical_identities = {_identity(target) for target in historical.targets.values()}
            if any(
                _identity(target) not in historical_identities
                for target in _may_participate(self._candidate, decision.request)
            ):
                return self._non_replayable(case, "availability_identity_missing")
            availability = decision.availability
        else:
            availability = _all_healthy(self._candidate, decision.availability.observed_at)
        self._verify_historical(case, historical)
        candidate_plan: ExecutionPlan | None = None
        candidate_error: RouterErrorSnapshot | None = None
        try:
            candidate_plan = self._kernel.plan(
                decision.request, RoutingContext(decision.session, availability)
            )
        except RouterError as error:
            candidate_error = RouterErrorSnapshot.from_error(error)
        return ReplayResult(
            request_id=decision.request_id,
            status=ReplayStatus.REPLAYED,
            historical_policy_hash=decision.routing_policy_hash,
            candidate_policy_hash=self._candidate.routing_policy_hash,
            actual_plan=decision.actual_plan,
            actual_error=decision.actual_error,
            candidate_plan=candidate_plan,
            candidate_error=candidate_error,
            change=_result_change(
                decision.actual_plan,
                decision.actual_error,
                candidate_plan,
                candidate_error,
            ),
            mode=self._mode.value,
        )

    def _non_replayable(self, case: ReplayCase, reason: str) -> ReplayResult:
        """Create one bounded compatibility result."""

        decision = case.decision
        return ReplayResult(
            request_id=decision.request_id,
            status=ReplayStatus.NON_REPLAYABLE,
            historical_policy_hash=decision.routing_policy_hash,
            candidate_policy_hash=self._candidate.routing_policy_hash,
            actual_plan=decision.actual_plan,
            actual_error=decision.actual_error,
            reason=reason,
            mode=self._mode.value,
        )

    @staticmethod
    def _verify_historical(case: ReplayCase, policy: RoutingPolicy) -> None:
        """Require production logic to reproduce the stored actual result."""

        decision = case.decision
        try:
            reproduced = RoutingKernel(policy).plan(
                decision.request,
                RoutingContext(decision.session, decision.availability),
            )
            if decision.actual_plan is None or encode_plan(reproduced) != encode_plan(decision.actual_plan):
                raise ReplayFatalError("historical_reproduction_mismatch")
        except RouterError as error:
            reproduced_error = RouterErrorSnapshot.from_error(error)
            if decision.actual_error is None or encode_error(reproduced_error) != encode_error(decision.actual_error):
                raise ReplayFatalError("historical_reproduction_mismatch") from error
