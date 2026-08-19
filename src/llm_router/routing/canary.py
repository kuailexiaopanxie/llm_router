"""Deterministic fail-open policy selection for controlled Canary routing."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Protocol

from llm_router.domain import RoutingRequest
from llm_router.evaluation.canary_models import (
    AffinityKind,
    CanaryAssignment,
    CanaryReason,
    PolicyRole,
)
from llm_router.routing.candidate import CandidateBundle
from llm_router.routing.kernel import RoutingKernel


class RoutingInvocationView(Protocol):
    """Expose only immutable invocation fields used by assignment."""

    @property
    def request_id(self) -> object:
        """Return the Router-owned request identity."""

    @property
    def task_id(self) -> object | None:
        """Return the optional task affinity identity."""

    @property
    def session_key(self) -> str | None:
        """Return the optional session affinity identity."""

    @property
    def request(self) -> RoutingRequest:
        """Return the sanitized routing request."""


@dataclass(frozen=True, slots=True)
class PolicySelection:
    """Bind one immutable Kernel to its auditable assignment."""

    kernel: RoutingKernel
    assignment: CanaryAssignment | None


@dataclass(frozen=True, slots=True)
class CanaryRuntimeState:
    """Record one startup-fixed Canary availability state."""

    active: bool
    reason: CanaryReason | None


class PolicySelectorPort(Protocol):
    """Choose one immutable routing policy for an invocation."""

    def select(self, invocation: RoutingInvocationView) -> PolicySelection:
        """Return a non-throwing selection with bounded audit metadata."""


class CurrentPolicySelector:
    """Always select the Current policy when Canary is not configured."""

    def __init__(self, kernel: RoutingKernel) -> None:
        """Bind the Current production Kernel."""

        self._kernel = kernel

    def select(self, invocation: RoutingInvocationView) -> PolicySelection:
        """Return Current without Canary assignment metadata."""

        return PolicySelection(self._kernel, None)


class CanaryPolicySelector:
    """Select Current or Candidate using fixed segments and HMAC affinity."""

    def __init__(
        self,
        current: RoutingKernel,
        candidate: CandidateBundle | None,
        state: CanaryRuntimeState,
        expected_hash: str,
        salt: bytes,
        threshold: int,
        segments: frozenset[tuple[object, str]],
    ) -> None:
        """Bind all startup-fixed selection inputs."""

        self._current = current
        self._candidate = candidate
        self._state = state
        self._expected_hash = expected_hash
        self._salt = salt
        self._threshold = threshold
        self._segments = segments

    def select(self, invocation: RoutingInvocationView) -> PolicySelection:
        """Choose one policy and fail open to Current on control faults."""

        try:
            candidate_hash = self._candidate.policy.routing_policy_hash if self._candidate else None
            if not self._state.active:
                return self._control(
                    self._state.reason or CanaryReason.CANDIDATE_UNAVAILABLE,
                    candidate_hash,
                )
            assert self._candidate is not None
            assert candidate_hash is not None
            request = invocation.request
            profile = request.requested_profile or self._current.policy.default_profile
            protocol = request.protocol
            if (protocol, profile) not in self._segments:
                return self._control(CanaryReason.SEGMENT_FILTERED, candidate_hash)
            if request.count_only:
                return self._control(CanaryReason.COUNT_ONLY_EXCLUDED, candidate_hash)
            kind, affinity = self._affinity(invocation)
            if request.response_state_requested and kind is AffinityKind.REQUEST:
                return self._control(CanaryReason.AFFINITY_REQUIRED, candidate_hash)
            bucket = self._bucket(candidate_hash, kind, affinity)
            role = PolicyRole.CANARY if bucket < self._threshold else PolicyRole.CONTROL
            reason = (
                CanaryReason.CANARY_BUCKET
                if role is PolicyRole.CANARY
                else CanaryReason.CONTROL_BUCKET
            )
            assignment = CanaryAssignment(
                role,
                reason,
                self._expected_hash,
                candidate_hash,
                kind,
                bucket,
                self._threshold,
            )
            kernel = self._candidate.kernel if role is PolicyRole.CANARY else self._current
            return PolicySelection(kernel, assignment)
        except Exception:  # noqa: BLE001 - selector is an explicit fail-open boundary.
            return self._control(CanaryReason.SELECTOR_FAILURE, None)

    def _control(
        self, reason: CanaryReason, candidate_hash: str | None
    ) -> PolicySelection:
        """Create one bounded Control assignment for an ineligible request."""

        return PolicySelection(
            self._current,
            CanaryAssignment(
                PolicyRole.CONTROL,
                reason,
                self._expected_hash,
                candidate_hash,
                AffinityKind.NONE,
                None,
                self._threshold,
            ),
        )

    @staticmethod
    def _affinity(invocation: RoutingInvocationView) -> tuple[AffinityKind, str]:
        """Choose opaque affinity in session, task, then request priority."""

        if invocation.session_key is not None:
            return AffinityKind.SESSION, invocation.session_key
        if invocation.task_id is not None:
            return AffinityKind.TASK, str(invocation.task_id)
        return AffinityKind.REQUEST, str(invocation.request_id)

    def _bucket(self, candidate_hash: str, kind: AffinityKind, affinity: str) -> int:
        """Compute the fixed HMAC-SHA256 bucket without retaining inputs."""

        message = json.dumps(
            [candidate_hash, kind.value, affinity], separators=(",", ":"), ensure_ascii=True
        ).encode()
        digest = hmac.new(self._salt, message, hashlib.sha256).digest()
        return int.from_bytes(digest[:8], "big") % 10_000
