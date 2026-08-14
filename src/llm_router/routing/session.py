"""Bounded in-process session escalation state."""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass

from llm_router.domain import OutcomeSignal, Tier


@dataclass
class SessionState:
    """Minimal state retained for one opt-in session ID."""

    last_tier: Tier = Tier.BALANCED
    last_outcome: OutcomeSignal = OutcomeSignal.UNKNOWN
    consecutive_failures: int = 0
    requests_since_failure: int = 0
    last_access: float = 0.0


class SessionStateStore:
    """TTL and capacity bounded session state with deterministic eviction."""

    def __init__(self, ttl_seconds: int, capacity: int) -> None:
        self._ttl = ttl_seconds
        self._capacity = capacity
        self._items: OrderedDict[str, SessionState] = OrderedDict()

    def _get(self, session_id: str) -> SessionState:
        """Get or create a live state entry and refresh its LRU position."""

        now = time.monotonic()
        state = self._items.get(session_id)
        if state is None or now - state.last_access > self._ttl:
            state = SessionState()
        state.last_access = now
        self._items[session_id] = state
        self._items.move_to_end(session_id)
        while len(self._items) > self._capacity:
            self._items.popitem(last=False)
        return state

    def snapshot(self, session_id: str | None) -> SessionState | None:
        """Return a copy of current state without creating anonymous sessions."""

        if not session_id:
            return None
        state = self._get(session_id)
        return SessionState(**state.__dict__)

    def record(self, session_id: str | None, tier: Tier, outcome: OutcomeSignal) -> None:
        """Update failure escalation state after a completed request."""

        if not session_id:
            return
        state = self._get(session_id)
        state.last_tier = tier
        state.last_outcome = outcome
        if outcome is OutcomeSignal.FAILURE:
            state.consecutive_failures += 1
            state.requests_since_failure = 0
        elif outcome is OutcomeSignal.SUCCESS:
            state.consecutive_failures = 0
            state.requests_since_failure = 0
        else:
            state.requests_since_failure += 1
            if state.requests_since_failure >= 2:
                state.consecutive_failures = max(0, state.consecutive_failures - 1)
                state.requests_since_failure = 0

