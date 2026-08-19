"""Private SQLite schema and read adapter for shadow comparisons."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from llm_router.evaluation.codec import encode_error, encode_plan, utc_text
from llm_router.evaluation.models import ShadowDecision
from llm_router.evaluation.shadow_codec import decode_shadow_row, shadow_payload_hash

SHADOW_SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow_decisions (
    request_id TEXT NOT NULL,
    candidate_policy_hash TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    protocol TEXT NOT NULL CHECK (protocol IN ('anthropic_messages','openai_responses')),
    requested_profile TEXT NOT NULL,
    actual_policy_hash TEXT NOT NULL,
    candidate_algorithm_version TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    status TEXT NOT NULL CHECK (status IN ('evaluated','non_replayable','evaluation_failed')),
    change TEXT CHECK (change IS NULL OR change IN ('unchanged','primary_changed','chain_changed','error_changed','plan_to_error','error_to_plan')),
    reason TEXT,
    actual_plan_json TEXT,
    actual_error_json TEXT,
    candidate_plan_json TEXT,
    candidate_error_json TEXT,
    payload_hash TEXT NOT NULL,
    PRIMARY KEY (request_id, candidate_policy_hash),
    CHECK ((actual_plan_json IS NULL) <> (actual_error_json IS NULL)),
    CHECK (
        (status = 'evaluated' AND ((candidate_plan_json IS NULL) <> (candidate_error_json IS NULL)) AND change IS NOT NULL AND reason IS NULL)
        OR
        (status <> 'evaluated' AND candidate_plan_json IS NULL AND candidate_error_json IS NULL AND change IS NULL AND reason IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_shadow_time_policy ON shadow_decisions(recorded_at, candidate_policy_hash);
CREATE INDEX IF NOT EXISTS idx_shadow_protocol ON shadow_decisions(protocol);
CREATE INDEX IF NOT EXISTS idx_shadow_status ON shadow_decisions(status);
CREATE INDEX IF NOT EXISTS idx_shadow_change ON shadow_decisions(change);
"""


def shadow_values(decision: ShadowDecision) -> tuple[object, ...]:
    """Return ordered SQL values for one canonical shadow decision."""

    return (
        str(decision.request_id),
        decision.candidate_policy_hash,
        utc_text(decision.recorded_at),
        utc_text(decision.evaluated_at),
        decision.protocol.value,
        decision.requested_profile,
        decision.actual_policy_hash,
        decision.candidate_algorithm_version,
        decision.schema_version,
        decision.status.value,
        decision.change.value if decision.change else None,
        decision.reason.value if decision.reason else None,
        encode_plan(decision.actual_plan) if decision.actual_plan else None,
        encode_error(decision.actual_error) if decision.actual_error else None,
        encode_plan(decision.candidate_plan) if decision.candidate_plan else None,
        encode_error(decision.candidate_error) if decision.candidate_error else None,
        shadow_payload_hash(decision),
    )


class SQLiteShadowReader:
    """Read bounded shadow decisions without opening a writable database."""

    def __init__(self, path: str) -> None:
        """Bind the reader to one expanded local SQLite path."""

        self._path = Path(path).expanduser()

    def iter_shadow(
        self,
        start: datetime | None,
        end: datetime | None,
        candidate_policy_hash: str | None,
        limit: int,
    ) -> Iterator[ShadowDecision]:
        """Yield typed decisions in stable time and policy order."""

        clauses: list[str] = []
        parameters: list[object] = []
        if start is not None:
            clauses.append("recorded_at >= ?")
            parameters.append(utc_text(start))
        if end is not None:
            clauses.append("recorded_at < ?")
            parameters.append(utc_text(end))
        if candidate_policy_hash is not None:
            clauses.append("candidate_policy_hash = ?")
            parameters.append(candidate_policy_hash)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        uri = f"file:{self._path}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            rows = connection.execute(
                f"SELECT * FROM shadow_decisions {where} "
                "ORDER BY recorded_at, candidate_policy_hash, request_id LIMIT ?",
                (*parameters, limit),
            )
            columns = [item[0] for item in rows.description]
            for raw in rows:
                yield decode_shadow_row(dict(zip(columns, raw)))

    def request_coverage(self, request_id: str) -> tuple[bool, bool]:
        """Return actual Outcome and Decision Input presence for one request."""

        uri = f"file:{self._path}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            outcome = connection.execute(
                "SELECT 1 FROM outcome_events WHERE request_id = ? LIMIT 1", (request_id,)
            ).fetchone()
            decision = connection.execute(
                "SELECT 1 FROM route_decision_inputs WHERE request_id = ? LIMIT 1", (request_id,)
            ).fetchone()
        return outcome is not None, decision is not None

    def iter_shadow_with_coverage(
        self,
        start: datetime | None,
        end: datetime | None,
        candidate_policy_hash: str | None,
        limit: int,
    ) -> Iterator[tuple[ShadowDecision, bool, bool]]:
        """Yield decisions plus actual Outcome and Decision capture presence."""

        clauses: list[str] = []
        parameters: list[object] = []
        if start is not None:
            clauses.append("recorded_at >= ?")
            parameters.append(utc_text(start))
        if end is not None:
            clauses.append("recorded_at < ?")
            parameters.append(utc_text(end))
        if candidate_policy_hash is not None:
            clauses.append("candidate_policy_hash = ?")
            parameters.append(candidate_policy_hash)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        uri = f"file:{self._path}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            rows = connection.execute(
                f"SELECT * FROM shadow_decisions {where} "
                "ORDER BY recorded_at, candidate_policy_hash, request_id LIMIT ?",
                (*parameters, limit),
            )
            columns = [item[0] for item in rows.description]
            for raw in rows:
                row = dict(zip(columns, raw))
                request_id = str(row["request_id"])
                outcome = connection.execute(
                    "SELECT 1 FROM outcome_events WHERE request_id = ? LIMIT 1", (request_id,)
                ).fetchone()
                captured = connection.execute(
                    "SELECT 1 FROM route_decision_inputs WHERE request_id = ? LIMIT 1", (request_id,)
                ).fetchone()
                yield decode_shadow_row(row), outcome is not None, captured is not None
