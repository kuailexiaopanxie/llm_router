"""Bounded aggregation and rendering for actual controlled Canary facts."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence


def _rate(numerator: int, denominator: int) -> dict[str, int | float | None]:
    """Return an observed rate with its explicit denominator."""

    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": numerator / denominator if denominator else None,
    }


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    """Return the nearest-rank percentile for bounded observed values."""

    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _integer(value: object) -> int:
    """Normalize one SQLite integer aggregate without accepting containers."""

    return int(value) if isinstance(value, (int, str)) else 0


def _number(value: object) -> float:
    """Normalize one optional SQLite numeric observation."""

    return float(value) if isinstance(value, (int, float, str)) else 0.0


def _role_report(rows: Sequence[dict[str, object]]) -> dict[str, object]:
    """Aggregate one actual policy role without counterfactual claims."""

    assigned = len(rows)
    completed_rows = [row for row in rows if row["execution_status"] is not None]
    successful = sum(row["execution_status"] == "success" for row in completed_rows)
    outcome_rows = [row for row in rows if _integer(row["outcome_count"]) > 0]
    conflicting = sum(bool(row["outcome_conflicting"]) for row in outcome_rows)
    comparable_outcomes = [row for row in outcome_rows if not row["outcome_conflicting"]]
    outcome_successes = sum(row["outcome_verdict"] == "success" for row in comparable_outcomes)
    latencies = [_number(row["total_latency_ms"]) for row in completed_rows]
    cost_statuses = Counter(str(row["cost_status"]) for row in completed_rows)
    known_costs: dict[str, int] = {}
    legacy_estimate = 0.0
    for row in completed_rows:
        currency = row["cost_currency"]
        amount = row["known_cost_nanos"]
        if isinstance(currency, str) and isinstance(amount, int):
            known_costs[currency] = known_costs.get(currency, 0) + amount
        if row["cost_status"] == "legacy_unknown":
            legacy_estimate += _number(row["legacy_estimated_cost"])
    return {
        "assigned": assigned,
        "planned": sum(bool(row["planned"]) for row in rows),
        "routing_errors": sum(bool(row["routing_error"]) for row in rows),
        "completed": len(completed_rows),
        "completion_gaps": assigned - len(completed_rows),
        "execution_statuses": dict(
            sorted(Counter(str(row["execution_status"]) for row in completed_rows).items())
        ),
        "execution_success": _rate(successful, len(completed_rows)),
        "outcome_coverage": _rate(len(outcome_rows), assigned),
        "outcome_conflicts": conflicting,
        "outcome_success": _rate(outcome_successes, len(comparable_outcomes)),
        "outcome_verdicts": dict(
            sorted(
                Counter(
                    str(row["outcome_verdict"])
                    for row in comparable_outcomes
                    if row["outcome_verdict"] is not None
                ).items()
            )
        ),
        "latency_ms": {
            "samples": len(latencies),
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
        },
        "input_tokens": sum(_integer(row["input_tokens"]) for row in completed_rows),
        "output_tokens": sum(_integer(row["output_tokens"]) for row in completed_rows),
        "known_cost_nanos": dict(sorted(known_costs.items())),
        "cost_coverage": dict(sorted(cost_statuses.items())),
        "legacy_estimate": legacy_estimate,
        "final_targets": dict(
            sorted(
                Counter(
                    str(row["final_model"])
                    for row in completed_rows
                    if row["final_model"] not in (None, "none")
                ).items()
            )
        ),
    }


def build_canary_report(rows: Sequence[dict[str, object]]) -> dict[str, object]:
    """Aggregate only observed assignment, execution, and Outcome facts."""

    roles = {
        role: _role_report([row for row in rows if row["role"] == role])
        for role in ("control", "canary")
    }
    reasons = Counter(str(row["reason"]) for row in rows)
    segments = Counter(f"{row['protocol']}|{row['profile']}|{row['role']}" for row in rows)
    hashes = sorted(
        {str(row["expected_candidate_policy_hash"]) for row in rows}
    )
    return {
        "selected": len(rows),
        "expected_candidate_policy_hashes": hashes,
        "roles": roles,
        "reasons": dict(sorted(reasons.items())),
        "segments": dict(sorted(segments.items())),
        "results": list(rows),
    }


def print_canary_table(report: dict[str, object]) -> None:
    """Print compact aggregates with explicit denominators and coverage."""

    print(f"selected={report['selected']}")
    print("ROLE     ASSIGNED COMPLETED GAPS ROUTE_ERRORS EXEC_SUCCESS OUTCOME_COVERAGE CONFLICTS")
    roles = report["roles"]
    assert isinstance(roles, dict)
    for role in ("control", "canary"):
        item = roles[role]
        assert isinstance(item, dict)
        execution = item["execution_success"]
        coverage = item["outcome_coverage"]
        assert isinstance(execution, dict) and isinstance(coverage, dict)
        execution_text = f"{execution['numerator']}/{execution['denominator']}"
        coverage_text = f"{coverage['numerator']}/{coverage['denominator']}"
        print(
            f"{role:<8} {item['assigned']:<8} {item['completed']:<9} "
            f"{item['completion_gaps']:<4} {item['routing_errors']:<12} "
            f"{execution_text:<12} {coverage_text:<16} {item['outcome_conflicts']}"
        )
