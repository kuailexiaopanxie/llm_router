"""Command-line adapters for server startup and read-only offline replay."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from collections.abc import Sequence

from llm_router.config import load_config
from llm_router.evaluation.codec import CodecError, parse_utc
from llm_router.evaluation.models import (
    ReplayChange,
    ReplayMode,
    ReplayResult,
    ReplayStatus,
)
from llm_router.evaluation.replay import ReplayEngine, ReplayFatalError
from llm_router.evaluation.sqlite_store import SQLiteReplayStore
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


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch replay only when it is the first command-line argument."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "replay":
        return run_replay(arguments[1:])
    from llm_router.app import main as server_main

    server_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
