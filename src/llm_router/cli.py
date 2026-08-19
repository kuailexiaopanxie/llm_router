"""Command-line adapters for server startup and read-only offline replay."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from collections.abc import Sequence

from llm_router.config import load_config
from llm_router.evaluation.canary_report import build_canary_report, print_canary_table
from llm_router.evaluation.canary_sqlite import SQLiteCanaryReader
from llm_router.evaluation.codec import CodecError, parse_utc
from llm_router.evaluation.models import (
    ReplayChange,
    ReplayMode,
    ReplayResult,
    ReplayStatus,
    ShadowDecision,
)
from llm_router.evaluation.replay import ReplayEngine, ReplayFatalError
from llm_router.evaluation.shadow_sqlite import SQLiteShadowReader
from llm_router.evaluation.sqlite_store import SQLiteReplayStore
from llm_router.observability.cli import run_cost, run_routes, run_trace
from llm_router.routing.policy import compile_routing_policy


def _replay_parser() -> argparse.ArgumentParser:
    """Build the bounded offline replay argument parser."""

    parser = argparse.ArgumentParser(prog="llm-router replay", description="Replay sanitized routing decisions")
    parser.add_argument("--db", required=True, help="Path to the router SQLite database")
    parser.add_argument("--candidate-config", required=True, help="Candidate router YAML")
    parser.add_argument("--from", dest="start", help="Inclusive RFC3339 UTC start")
    parser.add_argument("--to", dest="end", help="Exclusive RFC3339 UTC end")
    parser.add_argument("--mode", choices=[item.value for item in ReplayMode], default="historical")
    parser.add_argument("--format", choices=("table", "json"), default="table")
    parser.add_argument("--limit", type=int)
    return parser


def _shadow_report_parser() -> argparse.ArgumentParser:
    """Build the read-only shadow comparison report parser."""

    parser = argparse.ArgumentParser(
        prog="llm-router shadow-report", description="Report persisted shadow policy comparisons"
    )
    parser.add_argument("--db", required=True, help="Path to the router SQLite database")
    parser.add_argument("--from", dest="start", help="Inclusive RFC3339 UTC start")
    parser.add_argument("--to", dest="end", help="Exclusive RFC3339 UTC end")
    parser.add_argument("--candidate-hash", help="Optional candidate policy SHA-256")
    parser.add_argument("--format", choices=("table", "json"), default="table")
    parser.add_argument("--limit", type=int, default=10_000)
    return parser


def _canary_report_parser() -> argparse.ArgumentParser:
    """Build the read-only actual Canary report parser."""

    parser = argparse.ArgumentParser(
        prog="llm-router canary-report",
        description="Report observed controlled Canary routing facts",
    )
    parser.add_argument("--db", required=True, help="Path to the router SQLite database")
    parser.add_argument("--from", dest="start", help="Inclusive RFC3339 UTC start")
    parser.add_argument("--to", dest="end", help="Exclusive RFC3339 UTC end")
    parser.add_argument("--format", choices=("table", "json"), default="table")
    parser.add_argument("--limit", type=int, default=10_000)
    return parser


def _safe_result(result: ReplayResult) -> dict[str, object]:
    """Render only bounded replay fields and target identities."""

    return {
        "request_id": str(result.request_id),
        "status": result.status.value,
        "historical_policy_hash": result.historical_policy_hash,
        "candidate_policy_hash": result.candidate_policy_hash,
        "mode": result.mode,
        "change": result.change.value if result.change else None,
        "reason": result.reason,
        "actual_primary": result.actual_plan.primary.alias if result.actual_plan else None,
        "actual_error": result.actual_error.code if result.actual_error else None,
        "candidate_primary": result.candidate_plan.primary.alias if result.candidate_plan else None,
        "candidate_error": result.candidate_error.code if result.candidate_error else None,
    }


def _report(
    results: list[ReplayResult],
    selected: int,
    outcome_count: int,
    groups: Counter[str],
    conflicting: int,
    changed_with_outcome: int,
) -> dict[str, object]:
    """Aggregate plan changes without counterfactual quality claims."""

    changes = Counter(result.change.value for result in results if result.change is not None)
    reasons = Counter(result.reason for result in results if result.reason is not None)
    replayed = sum(result.status is ReplayStatus.REPLAYED for result in results)
    return {
        "selected": selected,
        "replayed": replayed,
        "non_replayable": selected - replayed,
        "outcome_covered": outcome_count,
        "conflicting_requests": conflicting,
        "changed_with_outcome": changed_with_outcome,
        "changes": dict(sorted(changes.items())),
        "groups": dict(sorted(groups.items())),
        "non_replayable_reasons": dict(sorted(reasons.items())),
        "results": [_safe_result(result) for result in results],
    }


def _print_table(report: dict[str, object]) -> None:
    """Print one compact human-readable replay report."""

    print(
        f"selected={report['selected']} replayed={report['replayed']} "
        f"non_replayable={report['non_replayable']} outcome_covered={report['outcome_covered']}"
    )
    print("REQUEST_ID                           STATUS          CHANGE/REASON")
    items = report["results"]
    assert isinstance(items, list)
    for item in items:
        assert isinstance(item, dict)
        detail = item["change"] or item["reason"] or "none"
        print(f"{item['request_id']:<36} {item['status']:<15} {detail}")


def run_replay(argv: Sequence[str]) -> int:
    """Execute one read-only replay invocation and return a stable exit code."""

    parser = _replay_parser()
    try:
        args = parser.parse_args(argv)
        config = load_config(args.candidate_config)
        limit = args.limit if args.limit is not None else config.replay.max_records
        if limit < 1 or limit > config.replay.max_records:
            parser.error(f"--limit must be between 1 and {config.replay.max_records}")
        start = parse_utc(args.start) if args.start else None
        end = parse_utc(args.end) if args.end else None
        if start is not None and end is not None and start >= end:
            parser.error("--from must be earlier than --to")
    except (TypeError, ValueError, CodecError) as exc:
        print(f"Replay configuration error: {exc}", file=sys.stderr)
        return 2
    engine = ReplayEngine(compile_routing_policy(config), args.mode)
    results: list[ReplayResult] = []
    selected = 0
    outcome_count = 0
    conflicting = 0
    changed_with_outcome = 0
    groups: Counter[str] = Counter()
    try:
        for case in SQLiteReplayStore(args.db).iter_cases(start, end, limit):
            selected += 1
            matched_outcomes = tuple(
                outcome
                for outcome in case.outcomes
                if not (
                    case.decision.task_id is not None
                    and outcome.task_id is not None
                    and case.decision.task_id != outcome.task_id
                )
            )
            if matched_outcomes:
                outcome_count += 1
                if len({outcome.verdict for outcome in matched_outcomes}) > 1:
                    conflicting += 1
            result = engine.replay(case)
            results.append(result)
            group_key = f"{case.decision.request.protocol.value}|{case.decision.request.requested_profile}|{result.change.value if result.change else result.reason}"
            groups[group_key] += 1
            if matched_outcomes and result.change not in (None, ReplayChange.UNCHANGED):
                changed_with_outcome += 1
    except (sqlite3.Error, OSError, CodecError, ReplayFatalError) as exc:
        print(f"Replay failed: {exc}", file=sys.stderr)
        return 3
    report = _report(results, selected, outcome_count, groups, conflicting, changed_with_outcome)
    if args.format == "json":
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    else:
        _print_table(report)
    return 0


def _safe_shadow(
    decision: ShadowDecision, outcome_covered: bool, captured: bool
) -> dict[str, object]:
    """Render only bounded persisted shadow result fields."""

    return {
        "request_id": str(decision.request_id),
        "candidate_policy_hash": decision.candidate_policy_hash,
        "protocol": decision.protocol.value,
        "requested_profile": decision.requested_profile,
        "status": decision.status.value,
        "change": decision.change.value if decision.change else None,
        "reason": decision.reason.value if decision.reason else None,
        "actual_primary": decision.actual_plan.primary.alias if decision.actual_plan else None,
        "actual_error": decision.actual_error.code if decision.actual_error else None,
        "candidate_primary": decision.candidate_plan.primary.alias if decision.candidate_plan else None,
        "candidate_error": decision.candidate_error.code if decision.candidate_error else None,
        "actual_outcome_covered": outcome_covered,
        "decision_captured": captured,
    }


def _shadow_report(rows: Sequence[tuple[ShadowDecision, bool, bool]]) -> dict[str, object]:
    """Aggregate persisted facts without counterfactual quality claims."""

    items = [_safe_shadow(*row) for row in rows]
    statuses = Counter(str(item["status"]) for item in items)
    changes = Counter(str(item["change"]) for item in items if item["change"] is not None)
    reasons = Counter(str(item["reason"]) for item in items if item["reason"] is not None)
    groups = Counter(f"{item['protocol']}|{item['requested_profile']}" for item in items)
    return {
        "selected": len(items),
        "statuses": dict(sorted(statuses.items())),
        "changes": dict(sorted(changes.items())),
        "reasons": dict(sorted(reasons.items())),
        "groups": dict(sorted(groups.items())),
        "actual_outcome_covered": sum(bool(item["actual_outcome_covered"]) for item in items),
        "decision_capture_gaps": sum(not bool(item["decision_captured"]) for item in items),
        "changed_with_actual_outcome": sum(
            bool(item["actual_outcome_covered"])
            and item["change"] not in (None, ReplayChange.UNCHANGED.value)
            for item in items
        ),
        "results": items,
    }


def _print_shadow_table(report: dict[str, object]) -> None:
    """Print one compact bounded shadow comparison report."""

    print(
        f"selected={report['selected']} actual_outcome_covered={report['actual_outcome_covered']} "
        f"decision_capture_gaps={report['decision_capture_gaps']}"
    )
    print("REQUEST_ID                           STATUS          CHANGE/REASON")
    items = report["results"]
    assert isinstance(items, list)
    for item in items:
        assert isinstance(item, dict)
        detail = item["change"] or item["reason"] or "none"
        print(f"{item['request_id']:<36} {item['status']:<15} {detail}")


def run_shadow_report(argv: Sequence[str]) -> int:
    """Read, aggregate, and render persisted shadow comparisons only."""

    parser = _shadow_report_parser()
    try:
        args = parser.parse_args(argv)
        if args.limit < 1 or args.limit > 100_000:
            parser.error("--limit must be between 1 and 100000")
        if args.candidate_hash is not None and not re.fullmatch(r"[0-9a-f]{64}", args.candidate_hash):
            parser.error("--candidate-hash must be a lowercase SHA-256")
        start = parse_utc(args.start) if args.start else None
        end = parse_utc(args.end) if args.end else None
        if start is not None and end is not None and start >= end:
            parser.error("--from must be earlier than --to")
    except (TypeError, ValueError, CodecError) as exc:
        print(f"Shadow report configuration error: {exc}", file=sys.stderr)
        return 2
    try:
        rows = list(
            SQLiteShadowReader(args.db).iter_shadow_with_coverage(
                start, end, args.candidate_hash, args.limit
            )
        )
    except (sqlite3.Error, OSError, CodecError) as exc:
        print(f"Shadow report failed: {exc}", file=sys.stderr)
        return 3
    report = _shadow_report(rows)
    if args.format == "json":
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    else:
        _print_shadow_table(report)
    return 0


def run_canary_report(argv: Sequence[str]) -> int:
    """Read and aggregate bounded actual Canary observations only."""

    parser = _canary_report_parser()
    try:
        args = parser.parse_args(argv)
        if args.limit < 1 or args.limit > 100_000:
            parser.error("--limit must be between 1 and 100000")
        start = parse_utc(args.start) if args.start else None
        end = parse_utc(args.end) if args.end else None
        if start is not None and end is not None and start >= end:
            parser.error("--from must be earlier than --to")
    except (TypeError, ValueError, CodecError) as exc:
        print(f"Canary report configuration error: {exc}", file=sys.stderr)
        return 2
    try:
        rows = SQLiteCanaryReader(args.db).rows(start, end, args.limit)
    except (sqlite3.Error, OSError, CodecError) as exc:
        print(f"Canary report failed: {exc}", file=sys.stderr)
        return 3
    report = build_canary_report(rows)
    if args.format == "json":
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    else:
        print_canary_table(report)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch replay only when it is the first command-line argument."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "replay":
        return run_replay(arguments[1:])
    if arguments and arguments[0] == "shadow-report":
        return run_shadow_report(arguments[1:])
    if arguments and arguments[0] == "canary-report":
        return run_canary_report(arguments[1:])
    if arguments and arguments[0] == "routes":
        return run_routes(arguments[1:])
    if arguments and arguments[0] == "trace":
        return run_trace(arguments[1:])
    if arguments and arguments[0] == "cost":
        return run_cost(arguments[1:])
    from llm_router.app import main as server_main

    server_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
