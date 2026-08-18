"""Validation and synchronous service for explicit Outcome Events."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from llm_router.evaluation.models import OutcomeEvent, OutcomeReceipt
from llm_router.evaluation.port import (
    EvaluationStoreError,
    OutcomeConflictError,
    OutcomeStorePort,
)


class OutcomeValidationError(ValueError):
    """Represent a bounded domain validation failure."""


class OutcomeUnavailableError(RuntimeError):
    """Represent an unavailable durable Outcome store."""


class OutcomeService:
    """Validate and synchronously persist client Outcome observations."""

    def __init__(
        self,
        store: OutcomeStorePort,
        max_event_age_seconds: int = 604_800,
        max_future_skew_seconds: int = 300,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Bind the store and bounded timestamp policy."""

        self._store = store
        self._max_age = timedelta(seconds=max_event_age_seconds)
        self._max_future = timedelta(seconds=max_future_skew_seconds)
        self._clock = clock

    async def submit(self, event: OutcomeEvent) -> OutcomeReceipt:
        """Validate one event and acknowledge only after transaction commit."""

        now = self._clock().astimezone(UTC)
        if event.observed_at is not None:
            if event.observed_at.tzinfo is None:
                raise OutcomeValidationError("observed_at must include a timezone")
            observed = event.observed_at.astimezone(UTC)
            if observed < now - self._max_age or observed > now + self._max_future:
                raise OutcomeValidationError("observed_at is outside the configured time window")
            event = replace(event, observed_at=observed)
        event = replace(event, received_at=now)
        try:
            return await self._store.submit_outcome(event)
        except OutcomeConflictError:
            raise
        except EvaluationStoreError as exc:
            raise OutcomeUnavailableError("outcome store is unavailable") from exc
