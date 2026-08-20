"""Short-lived read-only SQLite access with deadlines and bounded capacity."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import TypeVar


class DashboardQueryError(RuntimeError):
    """Represent one bounded dashboard query failure."""

    def __init__(self, code: str) -> None:
        """Store only a fixed public error code."""

        super().__init__(code)
        self.code = code


class DashboardSQLiteReader:
    """Create isolated read-only connections for dashboard queries."""

    def __init__(self, path: str, timeout_ms: int = 2000) -> None:
        """Bind a database path without creating or opening it."""

        self.path = Path(path).expanduser()
        self.timeout_ms = timeout_ms

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Open one deadline-bound deferred read transaction."""

        if not self.path.is_file():
            raise DashboardQueryError("observation_unavailable")
        connection: sqlite3.Connection | None = None
        deadline = time.monotonic() + self.timeout_ms / 1000
        try:
            uri = f"file:{self.path}?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=0.25)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA busy_timeout=250")
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            self._validate_schema(connection)
            connection.execute("BEGIN")
            yield connection
            connection.commit()
        except DashboardQueryError:
            if connection is not None:
                connection.rollback()
            raise
        except sqlite3.OperationalError as exc:
            if connection is not None:
                connection.rollback()
            code = "query_timeout" if "interrupt" in str(exc).lower() else "observation_unavailable"
            raise DashboardQueryError(code) from None
        except (sqlite3.DatabaseError, OSError):
            if connection is not None:
                connection.rollback()
            raise DashboardQueryError("observation_unavailable") from None
        finally:
            if connection is not None:
                connection.set_progress_handler(None, 0)
                connection.close()

    def capabilities(self, connection: sqlite3.Connection) -> dict[str, frozenset[str]]:
        """Detect fixed known tables and columns without accepting client identifiers."""

        names = (
            "route_requests", "route_attempts", "route_usage", "route_cost_items",
            "route_spans", "outcome_events", "route_decision_inputs",
        )
        result: dict[str, frozenset[str]] = {}
        for name in names:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()
            if exists:
                result[name] = frozenset(row[1] for row in connection.execute(f"PRAGMA table_info({name})"))
        return result

    def _validate_schema(self, connection: sqlite3.Connection) -> None:
        """Require the minimum v0.1 route request schema."""

        capabilities = self.capabilities(connection)
        required = {
            "request_id", "received_at", "protocol", "profile", "stream", "primary_model",
            "final_model", "route_reason", "policy_version", "status", "attempt_count",
            "total_latency_ms", "input_tokens", "output_tokens", "error_code",
        }
        if not required.issubset(capabilities.get("route_requests", frozenset())):
            raise DashboardQueryError("unsupported_schema")


T = TypeVar("T")


class DashboardQueryExecutor:
    """Run SQLite reads outside the event loop with fixed capacity."""

    def __init__(self) -> None:
        """Create two workers and capacity for eight queued requests."""

        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="llm-router-dashboard")
        self._capacity = asyncio.Semaphore(10)
        self._active = 0
        self._lock = threading.Lock()

    @property
    def active(self) -> int:
        """Return the current number of running thread jobs."""

        with self._lock:
            return self._active

    async def run(self, function: Callable[[], T]) -> T:
        """Schedule one bounded query or reject immediately when saturated."""

        if self._capacity.locked():
            raise DashboardQueryError("dashboard_busy")
        await self._capacity.acquire()
        loop = asyncio.get_running_loop()

        def invoke() -> T:
            """Track active query count around one synchronous read."""

            with self._lock:
                self._active += 1
            try:
                return function()
            finally:
                with self._lock:
                    self._active -= 1

        try:
            return await loop.run_in_executor(self._pool, invoke)
        finally:
            self._capacity.release()

    def close(self) -> None:
        """Release executor threads without waiting on application shutdown."""

        self._pool.shutdown(wait=False, cancel_futures=True)
