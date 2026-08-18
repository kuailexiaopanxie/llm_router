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
                task_id TEXT,
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
                error_code TEXT,
                inbound_protocol TEXT,
                target_protocol TEXT,
                provider_account_scope TEXT,
                response_state_requested INTEGER NOT NULL DEFAULT 0,
                translation_mode TEXT NOT NULL DEFAULT 'none',
                health_enabled INTEGER NOT NULL DEFAULT 0,
                health_snapshot_revision INTEGER NOT NULL DEFAULT 0,
                health_filtered_count INTEGER NOT NULL DEFAULT 0,
                health_skipped_count INTEGER NOT NULL DEFAULT 0,
                health_reason TEXT
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
        await self._ensure_columns(
            "route_requests",
            {
                "inbound_protocol": "TEXT",
                "target_protocol": "TEXT",
                "provider_account_scope": "TEXT",
                "response_state_requested": "INTEGER NOT NULL DEFAULT 0",
                "translation_mode": "TEXT NOT NULL DEFAULT 'none'",
                "health_enabled": "INTEGER NOT NULL DEFAULT 0",
                "health_snapshot_revision": "INTEGER NOT NULL DEFAULT 0",
                "health_filtered_count": "INTEGER NOT NULL DEFAULT 0",
                "health_skipped_count": "INTEGER NOT NULL DEFAULT 0",
                "health_reason": "TEXT",
                "task_id": "TEXT",
            },
        )
        await self._connection.commit()

    async def _ensure_columns(self, table: str, columns: dict[str, str]) -> None:
        """Add bounded telemetry columns when opening a v0.1 database."""

        assert self._connection is not None
        cursor = await self._connection.execute(f"PRAGMA table_info({table})")
        existing = {row[1] for row in await cursor.fetchall()}
        await cursor.close()
        for name, definition in columns.items():
            if name not in existing:
                await self._connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    async def append(self, event: RouteEvent) -> None:
        """Persist one sanitized route event and its attempt summaries."""

        if self._connection is None:
            raise RuntimeError("SQLite event store is not started")
        await self._connection.execute(
            """
            INSERT OR REPLACE INTO route_requests
            (request_id, task_id, received_at, protocol, profile, stream, feature_summary,
             primary_model, final_model, route_reason, policy_version, status,
             attempt_count, time_to_first_event_ms, total_latency_ms, input_tokens,
             output_tokens, estimated_cost, error_code, inbound_protocol, target_protocol,
             provider_account_scope, response_state_requested, translation_mode,
             health_enabled, health_snapshot_revision, health_filtered_count,
             health_skipped_count, health_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.request_id,
                event.task_id,
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
                event.inbound_protocol,
                event.target_protocol,
                event.provider_account_scope,
                int(event.response_state_requested),
                event.translation_mode,
                int(event.health_enabled),
                event.health_snapshot_revision,
                event.health_filtered_count,
                event.health_skipped_count,
                event.health_reason,
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
