"""SQLite adapters for durable evaluation writes and read-only replay."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

import aiosqlite

from llm_router.evaluation.canary_codec import (
    assignment_from_decision_row,
    encode_canary_assignment,
)
from llm_router.evaluation.codec import (
    CodecError,
    decode_availability,
    decode_error,
    decode_plan,
    decode_routing_request,
    decode_session,
    encode_availability,
    encode_error,
    encode_plan,
    encode_routing_request,
    encode_session,
    outcome_payload_hash,
    parse_utc,
    utc_text,
)
from llm_router.evaluation.models import (
    Correlation,
    OutcomeEvent,
    OutcomeEvidence,
    OutcomeReceipt,
    OutcomeSource,
    OutcomeVerdict,
    ReplayCase,
    RouteDecisionInput,
    RoutingPolicySnapshot,
    ShadowDecision,
)
from llm_router.evaluation.port import (
    EvaluationStoreError,
    OutcomeConflictError,
    ShadowIntegrityError,
)
from llm_router.evaluation.shadow_codec import validate_shadow_size
from llm_router.evaluation.shadow_sqlite import (
    SHADOW_SCHEMA,
    SQLiteShadowReader,
    shadow_values,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outcome_events (
    event_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    task_id TEXT,
    verdict TEXT NOT NULL CHECK (verdict IN ('success','failure','partial')),
    evidence TEXT NOT NULL CHECK (evidence IN ('patch_apply','compile','lint','test','tool','task')),
    source TEXT NOT NULL CHECK (source IN ('client','ide','ci','integration')),
    observed_at TEXT,
    received_at TEXT NOT NULL,
    payload_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outcome_request ON outcome_events(request_id);
CREATE INDEX IF NOT EXISTS idx_outcome_task ON outcome_events(task_id);
CREATE INDEX IF NOT EXISTS idx_outcome_observed ON outcome_events(observed_at);
CREATE TABLE IF NOT EXISTS routing_policy_snapshots (
    routing_policy_hash TEXT PRIMARY KEY,
    policy_version TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    routing_algorithm_version TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS route_decision_inputs (
    request_id TEXT PRIMARY KEY,
    task_id TEXT,
    recorded_at TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    router_version TEXT NOT NULL,
    routing_algorithm_version TEXT NOT NULL,
    routing_policy_hash TEXT NOT NULL,
    routing_request_json TEXT NOT NULL,
    session_snapshot_json TEXT,
    availability_snapshot_json TEXT NOT NULL,
    actual_plan_json TEXT,
    actual_error_json TEXT,
    CHECK ((actual_plan_json IS NULL) <> (actual_error_json IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_decision_time ON route_decision_inputs(recorded_at, request_id);
CREATE INDEX IF NOT EXISTS idx_decision_policy ON route_decision_inputs(routing_policy_hash);
CREATE INDEX IF NOT EXISTS idx_decision_task ON route_decision_inputs(task_id);
"""


class SQLiteEvaluationStore:
    """Serialize synchronous Outcome and asynchronous Decision writes."""

    def __init__(self, path: str) -> None:
        """Bind an evaluation adapter to the router SQLite path."""

        self._path = Path(path).expanduser()
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    async def start(self) -> None:
        """Open SQLite and apply additive evaluation migrations."""

        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(self._path, check_same_thread=False)
        await connection.execute("PRAGMA journal_mode=WAL")
        await connection.execute("PRAGMA synchronous=NORMAL")
        await connection.execute("PRAGMA busy_timeout=2000")
        await connection.executescript(_SCHEMA + SHADOW_SCHEMA)
        cursor = await connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='route_requests'"
        )
        route_table_exists = await cursor.fetchone() is not None
        await cursor.close()
        if route_table_exists:
            cursor = await connection.execute("PRAGMA table_info(route_requests)")
            columns = {row[1] for row in await cursor.fetchall()}
            await cursor.close()
            if "task_id" not in columns:
                await connection.execute("ALTER TABLE route_requests ADD COLUMN task_id TEXT")
        cursor = await connection.execute("PRAGMA table_info(route_decision_inputs)")
        decision_columns = {row[1] for row in await cursor.fetchall()}
        await cursor.close()
        if "canary_assignment_json" not in decision_columns:
            await connection.execute(
                "ALTER TABLE route_decision_inputs ADD COLUMN canary_assignment_json TEXT"
            )
        await connection.commit()
        self._connection = connection
        self._closed = False

    def _require_connection(self) -> aiosqlite.Connection:
        """Return the live connection or fail before acknowledging a write."""

        if self._connection is None or self._closed:
            raise EvaluationStoreError("evaluation store is unavailable")
        return self._connection

    async def ensure_policy(self, snapshot: RoutingPolicySnapshot) -> None:
        """Insert one immutable policy snapshot and verify hash integrity."""

        connection = self._require_connection()
        async with self._lock:
            await connection.execute(
                """
                INSERT OR IGNORE INTO routing_policy_snapshots
                (routing_policy_hash, policy_version, schema_version,
                 routing_algorithm_version, policy_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.routing_policy_hash,
                    snapshot.policy_version,
                    snapshot.schema_version,
                    snapshot.routing_algorithm_version,
                    snapshot.policy_json,
                    utc_text(snapshot.created_at),
                ),
            )
            cursor = await connection.execute(
                "SELECT policy_json FROM routing_policy_snapshots WHERE routing_policy_hash = ?",
                (snapshot.routing_policy_hash,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None or row[0] != snapshot.policy_json:
                await connection.rollback()
                raise EvaluationStoreError("routing policy snapshot integrity failure")
            await connection.commit()

    async def append_decision(self, decision: RouteDecisionInput) -> None:
        """Insert one decision without replacing immutable history."""

        connection = self._require_connection()
        async with self._lock:
            try:
                await connection.execute(
                    """
                    INSERT INTO route_decision_inputs
                    (request_id, task_id, recorded_at, schema_version, router_version,
                     routing_algorithm_version, routing_policy_hash, routing_request_json,
                     session_snapshot_json, availability_snapshot_json,
                     actual_plan_json, actual_error_json, canary_assignment_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(decision.request_id),
                        str(decision.task_id) if decision.task_id else None,
                        utc_text(decision.recorded_at),
                        decision.schema_version,
                        decision.router_version,
                        decision.routing_algorithm_version,
                        decision.routing_policy_hash,
                        encode_routing_request(decision.request),
                        encode_session(decision.session),
                        encode_availability(decision.availability),
                        encode_plan(decision.actual_plan) if decision.actual_plan else None,
                        encode_error(decision.actual_error) if decision.actual_error else None,
                        encode_canary_assignment(decision.canary_assignment),
                    ),
                )
                await connection.commit()
            except (aiosqlite.Error, CodecError) as exc:
                await connection.rollback()
                raise EvaluationStoreError("decision input could not be persisted") from exc

    async def _correlation(
        self, connection: aiosqlite.Connection, request_id: str, task_id: str | None
    ) -> tuple[Correlation, str | None]:
        """Calculate current request correlation from decision then route facts."""

        cursor = await connection.execute(
            "SELECT task_id FROM route_decision_inputs WHERE request_id = ?", (request_id,)
        )
        decision_row = await cursor.fetchone()
        await cursor.close()
        actual_target: str | None = None
        stored_task = decision_row[0] if decision_row is not None else None
        request_found = decision_row is not None
        if not request_found:
            cursor = await connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='route_requests'"
            )
            route_table_exists = await cursor.fetchone() is not None
            await cursor.close()
            if route_table_exists:
                cursor = await connection.execute(
                    "SELECT task_id, final_model, status FROM route_requests WHERE request_id = ?",
                    (request_id,),
                )
                route_row = await cursor.fetchone()
                await cursor.close()
                if route_row is not None:
                    stored_task = route_row[0]
                    request_found = True
                    if route_row[2] == "success" and route_row[1] != "none":
                        actual_target = route_row[1]
        if not request_found:
            return Correlation.PENDING, None
        if task_id is not None and stored_task is not None and task_id != stored_task:
            return Correlation.TASK_MISMATCH, actual_target
        return Correlation.MATCHED, actual_target

    async def submit_outcome(self, event: OutcomeEvent) -> OutcomeReceipt:
        """Atomically insert, deduplicate, or reject one Outcome Event."""

        connection = self._require_connection()
        digest = outcome_payload_hash(event)
        async with self._lock:
            try:
                await connection.execute("BEGIN IMMEDIATE")
                cursor = await connection.execute(
                    "SELECT payload_hash FROM outcome_events WHERE event_id = ?",
                    (str(event.event_id),),
                )
                existing = await cursor.fetchone()
                await cursor.close()
                if existing is not None and existing[0] != digest:
                    await connection.rollback()
                    raise OutcomeConflictError("outcome event id conflicts with existing payload")
                status = "duplicate" if existing is not None else "accepted"
                if existing is None:
                    await connection.execute(
                        """
                        INSERT INTO outcome_events
                        (event_id, request_id, task_id, verdict, evidence, source,
                         observed_at, received_at, payload_hash)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(event.event_id),
                            str(event.request_id),
                            str(event.task_id) if event.task_id else None,
                            event.verdict.value,
                            event.evidence.value,
                            event.source.value,
                            utc_text(event.observed_at),
                            utc_text(event.received_at),
                            digest,
                        ),
                    )
                correlation, actual_target = await self._correlation(
                    connection,
                    str(event.request_id),
                    str(event.task_id) if event.task_id else None,
                )
                await connection.commit()
                return OutcomeReceipt(event.event_id, status, correlation, actual_target)
            except OutcomeConflictError:
                raise
            except (aiosqlite.Error, CodecError) as exc:
                await connection.rollback()
                raise EvaluationStoreError("outcome event could not be persisted") from exc

    async def append_shadow(self, decision: ShadowDecision) -> Literal["written", "duplicate"]:
        """Insert one immutable shadow result with policy and payload checks."""

        connection = self._require_connection()
        validate_shadow_size(decision)
        values = shadow_values(decision)
        async with self._lock:
            try:
                await connection.execute("BEGIN IMMEDIATE")
                policy_cursor = await connection.execute(
                    "SELECT COUNT(*) FROM routing_policy_snapshots WHERE routing_policy_hash IN (?, ?)",
                    (decision.actual_policy_hash, decision.candidate_policy_hash),
                )
                policy_row = await policy_cursor.fetchone()
                policy_count = int(policy_row[0]) if policy_row is not None else 0
                await policy_cursor.close()
                if policy_count != (1 if decision.actual_policy_hash == decision.candidate_policy_hash else 2):
                    await connection.rollback()
                    raise ShadowIntegrityError("shadow policy snapshot is missing")
                cursor = await connection.execute(
                    "SELECT payload_hash FROM shadow_decisions WHERE request_id = ? AND candidate_policy_hash = ?",
                    (str(decision.request_id), decision.candidate_policy_hash),
                )
                existing = await cursor.fetchone()
                await cursor.close()
                if existing is not None:
                    if existing[0] != values[-1]:
                        await connection.rollback()
                        raise ShadowIntegrityError("shadow decision key conflicts with existing payload")
                    await connection.rollback()
                    return "duplicate"
                await connection.execute(
                    """
                    INSERT INTO shadow_decisions
                    (request_id, candidate_policy_hash, recorded_at, evaluated_at, protocol,
                     requested_profile, actual_policy_hash, candidate_algorithm_version,
                     schema_version, status, change, reason, actual_plan_json, actual_error_json,
                     candidate_plan_json, candidate_error_json, payload_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
                await connection.commit()
                return "written"
            except (ShadowIntegrityError, aiosqlite.IntegrityError):
                await connection.rollback()
                raise
            except (aiosqlite.Error, CodecError) as exc:
                await connection.rollback()
                raise EvaluationStoreError("shadow decision could not be persisted") from exc

    def iter_shadow(
        self,
        start: datetime | None,
        end: datetime | None,
        candidate_policy_hash: str | None,
        limit: int,
    ) -> Iterator[ShadowDecision]:
        """Yield shadow decisions through the read-only adapter."""

        yield from SQLiteShadowReader(str(self._path)).iter_shadow(
            start, end, candidate_policy_hash, limit
        )

    async def close(self) -> None:
        """Stop acknowledging writes and close the SQLite connection."""

        self._closed = True
        if self._connection is not None:
            await self._connection.close()
            self._connection = None


class SQLiteReplayStore:
    """Stream bounded replay cases from SQLite opened in read-only mode."""

    def __init__(self, path: str) -> None:
        """Open no connection until a bounded query begins."""

        self._path = Path(path).expanduser()

    def iter_cases(
        self, start: datetime | None, end: datetime | None, limit: int
    ) -> Iterator[ReplayCase]:
        """Yield replay cases in deterministic order without database writes."""

        uri = f"file:{self._path}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            clauses: list[str] = []
            parameters: list[object] = []
            if start is not None:
                clauses.append("recorded_at >= ?")
                parameters.append(utc_text(start))
            if end is not None:
                clauses.append("recorded_at < ?")
                parameters.append(utc_text(end))
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = connection.execute(
                f"SELECT * FROM route_decision_inputs {where} ORDER BY recorded_at, request_id LIMIT ?",
                (*parameters, limit),
            )
            columns = [item[0] for item in rows.description]
            for raw in rows:
                row = dict(zip(columns, raw))
                decision = _decode_decision(row)
                policy_row = connection.execute(
                    "SELECT * FROM routing_policy_snapshots WHERE routing_policy_hash = ?",
                    (decision.routing_policy_hash,),
                ).fetchone()
                policy = _decode_policy_row(policy_row) if policy_row else None
                yield ReplayCase(decision, policy, _read_outcomes(connection, decision.request_id))


def _decode_decision(row: dict[str, object]) -> RouteDecisionInput:
    """Decode one versioned decision row into its typed domain model."""

    return RouteDecisionInput(
        request_id=UUID(str(row["request_id"])),
        task_id=UUID(str(row["task_id"])) if row["task_id"] else None,
        recorded_at=parse_utc(row["recorded_at"]),
        router_version=str(row["router_version"]),
        routing_algorithm_version=str(row["routing_algorithm_version"]),
        routing_policy_hash=str(row["routing_policy_hash"]),
        request=decode_routing_request(str(row["routing_request_json"])),
        session=decode_session(str(row["session_snapshot_json"])) if row["session_snapshot_json"] else None,
        availability=decode_availability(str(row["availability_snapshot_json"])),
        actual_plan=decode_plan(str(row["actual_plan_json"])) if row["actual_plan_json"] else None,
        actual_error=decode_error(str(row["actual_error_json"])) if row["actual_error_json"] else None,
        canary_assignment=assignment_from_decision_row(row),
        schema_version=int(str(row["schema_version"])),
    )


def _decode_policy_row(row: tuple[object, ...]) -> RoutingPolicySnapshot:
    """Decode one immutable policy snapshot row."""

    return RoutingPolicySnapshot(
        routing_policy_hash=str(row[0]),
        policy_version=str(row[1]),
        schema_version=int(str(row[2])),
        routing_algorithm_version=str(row[3]),
        policy_json=str(row[4]),
        created_at=parse_utc(row[5]),
    )


def _read_outcomes(connection: sqlite3.Connection, request_id: UUID) -> tuple[OutcomeEvent, ...]:
    """Read actual Outcome observations for one historical request."""

    rows = connection.execute(
        "SELECT event_id, request_id, task_id, verdict, evidence, source, observed_at, received_at "
        "FROM outcome_events WHERE request_id = ? ORDER BY received_at, event_id",
        (str(request_id),),
    )
    return tuple(
        OutcomeEvent(
            UUID(row[0]),
            UUID(row[1]),
            UUID(row[2]) if row[2] else None,
            OutcomeVerdict(row[3]),
            OutcomeEvidence(row[4]),
            OutcomeSource(row[5]),
            parse_utc(row[6]) if row[6] else None,
            parse_utc(row[7]),
        )
        for row in rows
    )
