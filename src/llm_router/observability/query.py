"""Read-only bounded route, trace, and cost queries."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any


class ObservationQueryError(RuntimeError):
    """Represent an unsupported or unavailable observation database."""


class ObservationQuery:
    """Query observation tables without loading runtime configuration."""

    def __init__(self, path: str) -> None:
        """Bind one expanded SQLite path."""

        self._path = Path(path).expanduser()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open a verified read-only connection with mapping rows."""

        if not self._path.is_file():
            raise ObservationQueryError("observation database does not exist")
        try:
            connection = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(route_requests)")
            }
            if not {"request_id", "received_at", "status"}.issubset(columns):
                raise ObservationQueryError("observation schema is unsupported")
            yield connection
        except sqlite3.Error as exc:
            raise ObservationQueryError("observation database could not be read") from exc
        finally:
            if "connection" in locals():
                connection.close()

    @staticmethod
    def _limit(value: int) -> int:
        """Validate a bounded detail query limit."""

        if not 1 <= value <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        return value

    def routes(
        self,
        *,
        request_id: str | None = None,
        task_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        status: str | None = None,
        model: str | None = None,
        profile: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Return recent route rows using composable parameterized filters."""

        clauses: list[str] = []
        parameters: list[object] = []
        for column, value in (
            ("request_id", request_id),
            ("task_id", task_id),
            ("status", status),
            ("final_model", model),
            ("profile", profile),
        ):
            if value is not None:
                clauses.append(f"r.{column} = ?")
                parameters.append(value)
        if start is not None:
            clauses.append("r.received_at >= ?")
            parameters.append(start.isoformat())
        if end is not None:
            clauses.append("r.received_at < ?")
            parameters.append(end.isoformat())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            columns = self._columns(connection, "route_requests")
            if task_id is not None and "task_id" not in columns:
                return []
            optional = [
                self._optional(columns, name)
                for name in (
                    "task_id",
                    "input_tokens",
                    "output_tokens",
                    "known_cost_nanos",
                    "cost_currency",
                    "trace_id",
                    "terminal_stage",
                    "effective_profile",
                    "policy_hash",
                    "policy_role",
                    "assignment_reason",
                )
            ]
            cost_status = self._coverage(columns, "cost_status")
            usage_status = self._coverage(columns, "usage_status")
            sql = f"""
                SELECT r.received_at, r.request_id, r.protocol, r.profile,
                       r.primary_model, r.final_model, r.route_reason, r.attempt_count,
                       r.status, r.total_latency_ms, {', '.join(optional)},
                       {cost_status}, {usage_status}
                FROM route_requests r {where}
                ORDER BY r.received_at DESC, r.request_id DESC LIMIT ?
            """
            rows = connection.execute(
                sql, (*parameters, self._limit(limit))
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
        """Return one table's declared columns."""

        return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}

    @staticmethod
    def _optional(columns: set[str], name: str) -> str:
        """Select an optional column as null when reading a legacy schema."""

        return f"r.{name}" if name in columns else f"NULL AS {name}"

    @staticmethod
    def _coverage(columns: set[str], name: str) -> str:
        """Select coverage or mark legacy rows explicitly unknown."""

        return (
            f"COALESCE(r.{name}, 'legacy_unknown') AS {name}"
            if name in columns
            else f"'legacy_unknown' AS {name}"
        )

    def trace(
        self,
        *,
        request_id: str | None = None,
        trace_id: str | None = None,
        task_id: str | None = None,
        limit: int = 1000,
    ) -> dict[str, Any]:
        """Return persisted spans or an explicit capture gap."""

        if sum(value is not None for value in (request_id, trace_id, task_id)) != 1:
            raise ValueError("exactly one trace selector is required")
        clauses: list[str] = []
        parameters: list[object] = []
        if request_id is not None:
            clauses.append("r.request_id = ?")
            parameters.append(request_id)
        if trace_id is not None:
            clauses.append("r.trace_id = ?")
            parameters.append(trace_id)
        if task_id is not None:
            clauses.append("r.task_id = ?")
            parameters.append(task_id)
        with self._connect() as connection:
            columns = self._columns(connection, "route_requests")
            if task_id is not None and "task_id" not in columns:
                return {"gap": "request_not_found", "requests": [], "spans": []}
            trace_expression = "r.trace_id" if "trace_id" in columns else "NULL"
            captured_expression = "r.trace_captured" if "trace_captured" in columns else "0"
            if trace_id is not None and "trace_id" not in columns:
                return {"gap": "request_not_found", "requests": [], "spans": []}
            requests = connection.execute(
                f"""
                SELECT r.request_id, {trace_expression} AS trace_id,
                       {captured_expression} AS trace_captured
                FROM route_requests r WHERE {' AND '.join(clauses)}
                ORDER BY r.received_at DESC, r.request_id DESC LIMIT ?
                """,
                (*parameters, self._limit(limit)),
            ).fetchall()
            ids = [str(row["request_id"]) for row in requests]
            spans: Sequence[sqlite3.Row] = ()
            if ids and self._columns(connection, "route_spans"):
                placeholders = ",".join("?" for _ in ids)
                spans = connection.execute(
                    f"""
                    SELECT trace_id, span_id, parent_span_id, request_id, name,
                           started_at, duration_ms, status, attributes_json
                    FROM route_spans WHERE request_id IN ({placeholders})
                    ORDER BY started_at, span_id LIMIT ?
                    """,
                    (*ids, self._limit(limit)),
                ).fetchall()
        gap = "request_not_found" if not requests else "trace_not_captured" if not spans else None
        return {
            "gap": gap,
            "requests": [dict(row) for row in requests],
            "spans": [dict(row) for row in spans],
        }

    def cost(
        self,
        *,
        group_by: str,
        start: datetime | None = None,
        end: datetime | None = None,
        task_id: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Aggregate known cost and explicit coverage in SQL."""

        group_expressions = {
            "day": "substr(r.received_at, 1, 10)",
            "provider": "{provider_expression}",
            "model": "r.final_model",
            "profile": "r.profile",
            "task": "{task_expression}",
            "request": "r.request_id",
        }
        expression_template = group_expressions.get(group_by)
        if expression_template is None:
            raise ValueError("unsupported cost group")
        clauses: list[str] = []
        parameters: list[object] = []
        if start is not None:
            clauses.append("r.received_at >= ?")
            parameters.append(start.isoformat())
        if end is not None:
            clauses.append("r.received_at < ?")
            parameters.append(end.isoformat())
        if task_id is not None:
            clauses.append("r.task_id = ?")
            parameters.append(task_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            columns = self._columns(connection, "route_requests")
            if task_id is not None and "task_id" not in columns:
                return []
            attempt_columns = self._columns(connection, "route_attempts")
            invoked = (
                "a.upstream_invoked=1"
                if "upstream_invoked" in attempt_columns
                else "a.status <> 'health_skipped'"
            )
            provider_expression = "'unknown'"
            if attempt_columns:
                provider_expression = (
                    "COALESCE((SELECT a.provider FROM route_attempts a "
                    f"WHERE a.request_id=r.request_id AND {invoked} "
                    "ORDER BY a.sequence DESC LIMIT 1), 'unknown')"
                )
            task_expression = (
                "COALESCE(r.task_id, 'none')" if "task_id" in columns else "'none'"
            )
            expression = expression_template.format(
                provider_expression=provider_expression,
                task_expression=task_expression,
            )
            currency = (
                "COALESCE(r.cost_currency, 'unknown')"
                if "cost_currency" in columns
                else "'unknown'"
            )
            usage_status = "r.usage_status" if "usage_status" in columns else "NULL"
            cost_status = "r.cost_status" if "cost_status" in columns else "NULL"
            known_cost = "r.known_cost_nanos" if "known_cost_nanos" in columns else "NULL"
            unknown_attempts = "r.unknown_cost_attempts" if "unknown_cost_attempts" in columns else "0"
            usage_columns = self._columns(connection, "route_usage")

            def token_sum(kind: str) -> str:
                """Build one legacy-safe correlated token aggregate."""

                if not usage_columns:
                    return "0"
                return (
                    "COALESCE((SELECT SUM(u.tokens) FROM route_usage u "
                    f"WHERE u.request_id=r.request_id AND u.kind='{kind}'), 0)"
                )

            sql = f"""
            SELECT {expression} AS group_key,
                   {currency} AS currency,
                   COUNT(*) AS request_count,
                   SUM(CASE WHEN {usage_status}='complete' THEN 1 ELSE 0 END) AS usage_complete,
                   SUM(CASE WHEN {usage_status}='partial' THEN 1 ELSE 0 END) AS usage_partial,
                   SUM(CASE WHEN {usage_status} IN ('missing','invalid') OR {usage_status} IS NULL THEN 1 ELSE 0 END) AS usage_missing,
                   SUM(CASE WHEN {cost_status}='complete' THEN 1 ELSE 0 END) AS cost_complete,
                   SUM(CASE WHEN {cost_status}='partial' THEN 1 ELSE 0 END) AS cost_partial,
                   SUM(CASE WHEN {cost_status}='unpriced' THEN 1 ELSE 0 END) AS cost_unpriced,
                   SUM(CASE WHEN {cost_status} IN ('usage_missing','not_applicable') OR {cost_status} IS NULL THEN 1 ELSE 0 END) AS cost_missing,
                   SUM(COALESCE({known_cost}, 0)) AS known_amount_nanos,
                   SUM(CASE WHEN r.attempt_count > 1 THEN 1 ELSE 0 END) AS fallback_requests,
                   SUM(COALESCE({unknown_attempts}, 0)) AS unknown_invoked_attempts,
                   SUM({token_sum('input_uncached')}) AS input_uncached_tokens,
                   SUM({token_sum('input_cache_read')}) AS input_cache_read_tokens,
                   SUM({token_sum('input_cache_write')}) AS input_cache_write_tokens,
                   SUM({token_sum('output')}) AS output_tokens,
                   SUM({token_sum('reasoning_output')}) AS reasoning_output_tokens
            FROM route_requests r {where}
            GROUP BY group_key, currency
            ORDER BY group_key DESC, currency LIMIT ?
        """
            rows = connection.execute(
                sql, (*parameters, self._limit(limit))
            ).fetchall()
        return [dict(row) for row in rows]
