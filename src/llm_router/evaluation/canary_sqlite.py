"""Read-only Shadow gate and actual Canary observation queries."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from llm_router.canary_config import CanarySegmentConfig
from llm_router.evaluation.canary_codec import decode_canary_assignment
from llm_router.evaluation.codec import (
    decode_plan,
    decode_routing_request,
    parse_utc,
    utc_text,
)


@dataclass(frozen=True, slots=True)
class SegmentGateSummary:
    """Count persisted Shadow statuses for one declared segment."""

    evaluated: int = 0
    non_replayable: int = 0
    evaluation_failed: int = 0


class SQLiteCanaryGateReader:
    """Evaluate startup Shadow gates from SQLite opened read-only."""

    def __init__(self, path: str) -> None:
        """Bind one expanded SQLite path without opening it."""

        self._path = Path(path).expanduser()

    def summaries(
        self,
        start: datetime,
        end: datetime,
        current_policy_hash: str,
        candidate_policy_hash: str,
        segments: tuple[CanarySegmentConfig, ...],
    ) -> dict[tuple[str, str], SegmentGateSummary]:
        """Return per-segment counts using captured Current effective profiles."""

        keys = {(segment.protocol.value, segment.profile) for segment in segments}
        counts: dict[tuple[str, str], Counter[str]] = {key: Counter() for key in keys}
        uri = f"file:{self._path}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            rows = connection.execute(
                """
                SELECT s.protocol, s.status, d.actual_plan_json, d.routing_request_json,
                       p.policy_json
                FROM shadow_decisions AS s
                JOIN route_decision_inputs AS d ON d.request_id = s.request_id
                JOIN routing_policy_snapshots AS p
                  ON p.routing_policy_hash = s.actual_policy_hash
                WHERE s.recorded_at >= ? AND s.recorded_at < ?
                  AND s.actual_policy_hash = ? AND s.candidate_policy_hash = ?
                ORDER BY s.recorded_at, s.request_id
                """,
                (
                    utc_text(start),
                    utc_text(end),
                    current_policy_hash,
                    candidate_policy_hash,
                ),
            )
            for protocol, status, plan_json, request_json, policy_json in rows:
                profile = self._profile(plan_json, request_json, policy_json)
                key = (str(protocol), profile) if profile is not None else None
                if key in counts:
                    counts[key][str(status)] += 1
        return {
            key: SegmentGateSummary(
                evaluated=value["evaluated"],
                non_replayable=value["non_replayable"],
                evaluation_failed=value["evaluation_failed"],
            )
            for key, value in counts.items()
        }

    @staticmethod
    def _profile(
        plan_payload: str | None, request_payload: str, policy_payload: str
    ) -> str | None:
        """Derive Current effective profile without trusting Shadow defaults."""

        try:
            if plan_payload:
                profile = json.loads(plan_payload).get("profile")
                if isinstance(profile, str) and profile:
                    return profile
            requested = json.loads(request_payload).get("requested_profile")
            if isinstance(requested, str) and requested:
                return requested
            default = json.loads(policy_payload).get("default_profile")
            return default if isinstance(default, str) and default else None
        except (AttributeError, TypeError, json.JSONDecodeError):
            return None


class SQLiteCanaryReader:
    """Read bounded actual Canary facts without mutating SQLite."""

    def __init__(self, path: str) -> None:
        """Bind one expanded SQLite path."""

        self._path = Path(path).expanduser()

    def rows(
        self, start: datetime | None, end: datetime | None, limit: int
    ) -> list[dict[str, object]]:
        """Return bounded assignment, route, and actual Outcome facts."""

        clauses = ["d.schema_version = 2", "d.canary_assignment_json IS NOT NULL"]
        parameters: list[object] = []
        if start is not None:
            clauses.append("d.recorded_at >= ?")
            parameters.append(utc_text(start))
        if end is not None:
            clauses.append("d.recorded_at < ?")
            parameters.append(utc_text(end))
        uri = f"file:{self._path}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            query = f"""
                SELECT d.request_id, d.recorded_at, d.canary_assignment_json,
                       d.routing_request_json, d.actual_plan_json, d.actual_error_json,
                       r.status, r.primary_model, r.final_model, r.attempt_count,
                       r.total_latency_ms, r.input_tokens, r.output_tokens, r.estimated_cost,
                       r.error_code,
                       (SELECT COUNT(*) FROM outcome_events o
                         WHERE o.request_id = d.request_id
                           AND NOT (d.task_id IS NOT NULL AND o.task_id IS NOT NULL
                                    AND d.task_id <> o.task_id)),
                       (SELECT COUNT(DISTINCT o.verdict) FROM outcome_events o
                         WHERE o.request_id = d.request_id
                           AND NOT (d.task_id IS NOT NULL AND o.task_id IS NOT NULL
                                    AND d.task_id <> o.task_id)),
                       (SELECT MIN(o.verdict) FROM outcome_events o
                         WHERE o.request_id = d.request_id
                           AND NOT (d.task_id IS NOT NULL AND o.task_id IS NOT NULL
                                    AND d.task_id <> o.task_id))
                FROM route_decision_inputs d
                LEFT JOIN route_requests r ON r.request_id = d.request_id
                WHERE {' AND '.join(clauses)}
                ORDER BY d.recorded_at, d.request_id LIMIT ?
            """
            result: list[dict[str, object]] = []
            for row in connection.execute(query, (*parameters, limit)):
                assignment = decode_canary_assignment(row[2])
                if assignment is None:
                    continue
                request = decode_routing_request(row[3])
                plan = decode_plan(row[4]) if row[4] is not None else None
                result.append(
                    {
                        "request_id": row[0],
                        "recorded_at": parse_utc(row[1]).isoformat(),
                        "role": assignment.role.value,
                        "reason": assignment.reason.value,
                        "expected_candidate_policy_hash": (
                            assignment.expected_candidate_policy_hash
                        ),
                        "candidate_policy_hash": assignment.candidate_policy_hash,
                        "affinity_kind": assignment.affinity_kind.value,
                        "bucket": assignment.bucket,
                        "threshold": assignment.threshold,
                        "protocol": request.protocol.value,
                        "profile": plan.profile if plan else request.requested_profile,
                        "planned": plan is not None,
                        "routing_error": row[5] is not None,
                        "planned_primary": plan.primary.alias if plan else None,
                        "planned_fallbacks": [item.alias for item in plan.fallbacks] if plan else [],
                        "execution_status": row[6],
                        "primary_model": row[7],
                        "final_model": row[8],
                        "attempt_count": row[9],
                        "total_latency_ms": row[10],
                        "input_tokens": row[11],
                        "output_tokens": row[12],
                        "estimated_cost": row[13],
                        "execution_error": row[14],
                        "outcome_count": row[15],
                        "outcome_conflicting": int(row[16] or 0) > 1,
                        "outcome_verdict": row[17] if int(row[16] or 0) == 1 else None,
                    }
                )
            return result
