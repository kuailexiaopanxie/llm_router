"""Bounded in-memory observation status for local operational views."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class ObservationRuntimeStatus:
    """Expose counters whose lifetime is the current process."""

    queue_depth: int
    queue_capacity: int
    dropped_since_start: int
    sqlite_failures_since_start: int
    otlp_failures_since_start: int
    updated_at: datetime


class ObservationRuntimeState:
    """Track bounded observation status since process start."""

    def __init__(self, queue_capacity: int) -> None:
        """Initialize all monotonic counters to zero."""

        self._lock = threading.Lock()
        self._queue_capacity = queue_capacity
        self._queue_depth = 0
        self._dropped = 0
        self._sqlite_failures = 0
        self._otlp_failures = 0
        self._updated_at = datetime.now(UTC)

    def queue(self, depth: int) -> None:
        """Record the latest bounded durable queue depth."""

        with self._lock:
            self._queue_depth = max(0, min(depth, self._queue_capacity))
            self._updated_at = datetime.now(UTC)

    def dropped(self, count: int = 1) -> None:
        """Increase the process-local dropped observation count."""

        with self._lock:
            self._dropped += max(0, count)
            self._updated_at = datetime.now(UTC)

    def sqlite_failed(self) -> None:
        """Increase the process-local SQLite sink failure count."""

        with self._lock:
            self._sqlite_failures += 1
            self._updated_at = datetime.now(UTC)

    def otlp_failed(self) -> None:
        """Increase the process-local OTLP failure count."""

        with self._lock:
            self._otlp_failures += 1
            self._updated_at = datetime.now(UTC)

    def snapshot(self) -> ObservationRuntimeStatus:
        """Return an immutable status snapshot without changing sinks."""

        with self._lock:
            return ObservationRuntimeStatus(
                self._queue_depth, self._queue_capacity, self._dropped,
                self._sqlite_failures, self._otlp_failures, self._updated_at,
            )
