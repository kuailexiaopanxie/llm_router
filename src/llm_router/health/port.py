"""Stable port for provider and target availability management."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from llm_router.domain import ModelTarget
from llm_router.health.models import AttemptOutcome, AvailabilitySnapshot, HealthLease


class HealthPort(Protocol):
    """Coordinate health snapshots, attempt admission, and bounded outcomes."""

    def snapshot(self, now: datetime) -> AvailabilitySnapshot:
        """Return one immutable availability view."""

    def acquire(self, target: ModelTarget, now: datetime) -> HealthLease | None:
        """Atomically admit a healthy call or one recovery probe."""

    def record(self, lease: HealthLease, outcome: AttemptOutcome) -> None:
        """Apply one sanitized attempt outcome."""
