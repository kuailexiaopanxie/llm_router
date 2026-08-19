"""Argument parsing for read-only observation commands."""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from llm_router.evaluation.codec import parse_utc
from llm_router.observability.query import ObservationQuery, ObservationQueryError
from llm_router.observability.renderers import (
    render_json,
    render_table,
    render_trace_tree,
)

_DEFAULT_DB = str(Path("~/.llm-router/router.db").expanduser())


def _common(
    parser: argparse.ArgumentParser,
    formats: tuple[str, ...] = ("table", "json"),
    default_format: str = "table",
) -> None:
    """Add common read-only output and database arguments."""

    parser.add_argument("--db", default=_DEFAULT_DB)
    parser.add_argument("--format", choices=formats, default=default_format)
    parser.add_argument("--limit", type=int, default=1000)


def _window(parser: argparse.ArgumentParser) -> None:
    """Add an optional left-closed UTC time window."""

    parser.add_argument("--from", dest="start")
    parser.add_argument("--to", dest="end")


def _parse_time(value: str | None) -> datetime | None:
    """Parse one optional RFC3339 value as UTC."""

    return parse_utc(value).astimezone(UTC) if value else None


def _uuid(value: str | None, field: str) -> str | None:
    """Normalize an optional UUID selector."""

    if value is None:
        return None
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise ValueError(f"{field} must be a UUID") from exc


def _exit_code(error: SystemExit) -> int:
    """Normalize argparse termination into the stable CLI exit contract."""

    return error.code if isinstance(error.code, int) else 2


def _routes_parser() -> argparse.ArgumentParser:
    """Build the routes query parser."""

    parser = argparse.ArgumentParser(prog="llm-router routes")
    _common(parser)
    _window(parser)
    parser.add_argument("--last", type=int)
    parser.add_argument("--request")
    parser.add_argument("--task")
    parser.add_argument("--status", choices=("success", "error", "cancelled", "abandoned"))
    parser.add_argument("--model")
    parser.add_argument("--profile")
    return parser


def run_routes(argv: Sequence[str]) -> int:
    """Run one bounded recent-route query."""

    try:
        args = _routes_parser().parse_args(argv)
        start = _parse_time(args.start)
        end = _parse_time(args.end)
        if args.last is not None:
            if args.last < 1:
                raise ValueError("last must be positive")
            end = datetime.now(UTC)
            start = end - timedelta(minutes=args.last)
        if start is not None and end is not None and start >= end:
            raise ValueError("from must be earlier than to")
        filters = {
            "request_id": _uuid(args.request, "request"),
            "task_id": _uuid(args.task, "task"),
            "start": start,
            "end": end,
            "status": args.status,
            "model": args.model,
            "profile": args.profile,
            "limit": args.limit,
        }
        rows = ObservationQuery(args.db).routes(**filters)
    except SystemExit as exc:
        return _exit_code(exc)
    except (ValueError, ObservationQueryError, sqlite3.Error) as exc:
        print(f"Routes query failed: {exc}", file=sys.stderr)
        return 3 if isinstance(exc, (ObservationQueryError, sqlite3.Error)) else 2
    if args.format == "json":
        print(render_json("rows", rows, filters))
    else:
        print(
            render_table(
                rows,
                (
                    "received_at",
                    "request_id",
                    "profile",
                    "effective_profile",
                    "policy_role",
                    "final_model",
                    "status",
                    "cost_status",
                    "trace_id",
                ),
            )
        )
    return 0


def _trace_parser() -> argparse.ArgumentParser:
    """Build the trace query parser."""

    parser = argparse.ArgumentParser(prog="llm-router trace")
    _common(parser, ("tree", "json"), "tree")
    parser.add_argument("--request")
    parser.add_argument("--trace")
    parser.add_argument("--task")
    return parser


def run_trace(argv: Sequence[str]) -> int:
    """Run one persisted trace query."""

    try:
        args = _trace_parser().parse_args(argv)
        trace_id = args.trace
        if trace_id is not None and not re.fullmatch(r"[0-9a-f]{32}", trace_id):
            raise ValueError("trace must be 32 lowercase hexadecimal characters")
        filters = {
            "request_id": _uuid(args.request, "request"),
            "trace_id": trace_id,
            "task_id": _uuid(args.task, "task"),
            "limit": args.limit,
        }
        payload = ObservationQuery(args.db).trace(**filters)
    except SystemExit as exc:
        return _exit_code(exc)
    except (ValueError, ObservationQueryError, sqlite3.Error) as exc:
        print(f"Trace query failed: {exc}", file=sys.stderr)
        return 3 if isinstance(exc, (ObservationQueryError, sqlite3.Error)) else 2
    print(render_json("trace", payload, filters) if args.format == "json" else render_trace_tree(payload))
    return 0


def _cost_parser() -> argparse.ArgumentParser:
    """Build the cost aggregation parser."""

    parser = argparse.ArgumentParser(prog="llm-router cost")
    _common(parser)
    _window(parser)
    parser.add_argument("--today", action="store_true")
    parser.add_argument("--task")
    parser.add_argument("--group-by", choices=("day", "provider", "model", "profile", "task", "request"), default="day")
    return parser


def run_cost(argv: Sequence[str]) -> int:
    """Run one SQL-side known-cost aggregation."""

    try:
        args = _cost_parser().parse_args(argv)
        start = _parse_time(args.start)
        end = _parse_time(args.end)
        if args.today:
            start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1)
        if start is not None and end is not None and start >= end:
            raise ValueError("from must be earlier than to")
        filters = {
            "group_by": args.group_by,
            "start": start,
            "end": end,
            "task_id": _uuid(args.task, "task"),
            "limit": args.limit,
        }
        groups = ObservationQuery(args.db).cost(**filters)
    except SystemExit as exc:
        return _exit_code(exc)
    except (ValueError, ObservationQueryError, sqlite3.Error) as exc:
        print(f"Cost query failed: {exc}", file=sys.stderr)
        return 3 if isinstance(exc, (ObservationQueryError, sqlite3.Error)) else 2
    if args.format == "json":
        print(render_json("groups", groups, filters))
    else:
        print(render_table(groups, ("group_key", "currency", "request_count", "known_amount_nanos", "cost_complete", "cost_partial", "cost_unpriced", "cost_missing")))
    return 0
