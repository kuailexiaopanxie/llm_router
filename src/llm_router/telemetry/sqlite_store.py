"""SQLite WAL event store for sanitized route observations."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import aiosqlite

from llm_router.domain import RouteEvent


class SQLiteEventStore:
    """Serialize route events through one SQLite connection."""

    def __init__(self, path: str) -> None:
        self._path = Path(path).expanduser()
        self._connection: aiosqlite.Connection | None = None

    async def start(self) -> None:
        """Create the database, enable WAL, and initialize sanitized tables."""

        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(self._path, check_same_thread=False)
        await self._connection.execute("PRAGMA journal_mode=WAL")
        await self._connection.execute("PRAGMA synchronous=NORMAL")
        await self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS route_requests (
                request_id TEXT PRIMARY KEY,
                received_at TEXT NOT NULL,
                protocol TEXT NOT NULL,
                profile TEXT NOT NULL,
                stream INTEGER NOT NULL,
                feature_summary TEXT NOT NULL,
                primary_model TEXT NOT NULL,
                final_model TEXT NOT NULL,
                route_reason TEXT NOT NULL,
                policy_version TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL,
                time_to_first_event_ms REAL,
                total_latency_ms REAL NOT NULL,
                input_tokens INTEGER,
                output_tokens INTEGER,
                estimated_cost REAL,
                error_code TEXT
            );
            CREATE TABLE IF NOT EXISTS route_attempts (
                request_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                started_at TEXT NOT NULL,
                duration_ms REAL NOT NULL,
                status TEXT NOT NULL,
                http_status INTEGER,
                error_code TEXT,
                PRIMARY KEY (request_id, sequence)
            );
            """
        )
        await self._connection.commit()

    async def append(self, event: RouteEvent) -> None:
        """Persist one sanitized route event and its attempt summaries."""

        if self._connection is None:
            raise RuntimeError("SQLite event store is not started")
        await self._connection.execute(
            """
            INSERT OR REPLACE INTO route_requests
            (request_id, received_at, protocol, profile, stream, feature_summary,
             primary_model, final_model, route_reason, policy_version, status,
             attempt_count, time_to_first_event_ms, total_latency_ms, input_tokens,
             output_tokens, estimated_cost, error_code)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.request_id,
                event.received_at.isoformat(),
                event.protocol,
                event.profile,
                int(event.stream),
                json.dumps(asdict(event.feature_summary), sort_keys=True),
                event.primary_model,
                event.final_model,
                event.route_reason,
                event.policy_version,
                event.status,
                event.attempt_count,
                event.time_to_first_event_ms,
                event.total_latency_ms,
                event.input_tokens,
                event.output_tokens,
                event.estimated_cost,
                event.error_code,
            ),
        )
        await self._connection.executemany(
            """
            INSERT OR REPLACE INTO route_attempts
            (request_id, sequence, provider, model, started_at, duration_ms, status, http_status, error_code)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    attempt.request_id,
                    attempt.sequence,
                    attempt.provider,
                    attempt.model,
                    attempt.started_at.isoformat(),
                    attempt.duration_ms,
                    attempt.status,
                    attempt.http_status,
                    attempt.error_code,
                )
                for attempt in event.attempts
            ],
        )
        await self._connection.commit()

    async def close(self) -> None:
        """Close the database connection after queued writes drain."""

        if self._connection is not None:
            await self._connection.close()
            self._connection = None

