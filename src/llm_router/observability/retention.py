"""Bounded observation-only SQLite retention worker."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite

from llm_router.observability.metrics import RouterMetrics


class RetentionWorker:
    """Delete expired observation bundles in short bounded transactions."""

    def __init__(self, path: str, retention_days: int, metrics: RouterMetrics) -> None:
        """Bind one database and immutable UTC retention window."""

        self._path = Path(path).expanduser()
        self._days = retention_days
        self._metrics = metrics
        self._stop = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._logger = logging.getLogger("llm_router.observability")

    async def start(self) -> None:
        """Start the daily best-effort retention loop."""

        self._worker = asyncio.create_task(self._run(), name="llm-router-retention")

    async def _run(self) -> None:
        """Run one bounded batch per tick until shutdown."""

        while not self._stop.is_set():
            await self.run_batch(datetime.now(UTC))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=86_400)
            except asyncio.TimeoutError:
                continue

    async def run_batch(self, now: datetime) -> int:
        """Delete at most 1000 expired request bundles and return their count."""

        cutoff = (now.astimezone(UTC) - timedelta(days=self._days)).isoformat()
        try:
            async with aiosqlite.connect(self._path) as connection:
                await connection.execute("PRAGMA busy_timeout=1000")
                await connection.execute("BEGIN IMMEDIATE")
                cursor = await connection.execute(
                    "SELECT request_id FROM route_requests WHERE received_at < ? ORDER BY received_at LIMIT 1000",
                    (cutoff,),
                )
                ids = [str(row[0]) for row in await cursor.fetchall()]
                await cursor.close()
                if not ids:
                    await connection.rollback()
                    self._update_size()
                    return 0
                placeholders = ",".join("?" for _ in ids)
                for table in (
                    "route_spans",
                    "route_cost_items",
                    "route_usage",
                    "route_attempts",
                    "route_requests",
                ):
                    await connection.execute(
                        f"DELETE FROM {table} WHERE request_id IN ({placeholders})", ids
                    )
                await connection.commit()
            self._update_size()
            self._logger.info(
                "Observation retention batch completed",
                extra={"event": "observation_retention_completed"},
            )
            return len(ids)
        except (aiosqlite.Error, OSError):
            self._metrics.observation_sink_failures.labels(
                "retention", "delete_failed"
            ).inc()
            self._logger.error(
                "Observation retention batch failed",
                extra={"event": "observation_retention_failed"},
            )
            return 0

    def _update_size(self) -> None:
        """Refresh the low-frequency database size gauge."""

        try:
            self._metrics.observation_store_size.set(self._path.stat().st_size)
        except OSError:
            self._metrics.observation_sink_failures.labels(
                "retention", "size_failed"
            ).inc()

    async def close(self) -> None:
        """Stop the retention task without delaying shutdown."""

        self._stop.set()
        if self._worker is not None:
            await self._worker
