"""Immutable runtime facts supplied to the pure routing kernel."""

from __future__ import annotations

from dataclasses import dataclass

from llm_router.domain import OutcomeSignal, Tier
from llm_router.health.models import AvailabilitySnapshot


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    """Expose only session facts used by one routing decision."""

    last_tier: Tier
    last_outcome: OutcomeSignal
    consecutive_failures: int
    requests_since_failure: int


@dataclass(frozen=True, slots=True)
class RoutingContext:
    """Combine immutable facts used by one routing decision."""

    session: SessionSnapshot | None
    availability: AvailabilitySnapshot

