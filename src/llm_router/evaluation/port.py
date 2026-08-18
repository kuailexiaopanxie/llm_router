"""Role-specific persistence ports for the evaluation subsystem."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Protocol

from llm_router.evaluation.models import (
    OutcomeEvent,
    OutcomeReceipt,
    ReplayCase,
    RouteDecisionInput,
    RoutingPolicySnapshot,
)


class EvaluationStoreError(RuntimeError):
    """Represent a bounded persistence or integrity failure."""


class OutcomeConflictError(EvaluationStoreError):
    """Report a reused event ID with different semantic content."""


class DecisionStorePort(Protocol):
    """Persist policy snapshots and sanitized decision inputs."""

    async def ensure_policy(self, snapshot: RoutingPolicySnapshot) -> None:
        """Ensure an immutable policy snapshot exists."""

    async def append_decision(self, decision: RouteDecisionInput) -> None:
        """Insert one immutable decision record."""


class OutcomeStorePort(Protocol):
    """Atomically submit bounded Outcome Events."""

    async def submit_outcome(self, event: OutcomeEvent) -> OutcomeReceipt:
        """Insert or compare one idempotent Outcome Event."""


class ReplayStorePort(Protocol):
    """Read bounded historical replay cases without mutations."""

    def iter_cases(
        self, start: datetime | None, end: datetime | None, limit: int
    ) -> Iterator[ReplayCase]:
        """Yield replay cases in stable time and request order."""
