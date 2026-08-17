"""Protocol-neutral helpers for bounded routing features."""

from __future__ import annotations

from llm_router.domain import FeatureSummary, RoutingRequest, TaskSignals


def bucket(value: int, limits: tuple[int, ...]) -> str:
    """Map a count to a stable bounded range label."""

    for limit in limits:
        if value <= limit:
            return f"le_{limit}"
    return f"gt_{limits[-1]}"


def classify_task_text(text: str) -> TaskSignals:
    """Classify a bounded set of generic task signals from ephemeral text."""

    normalized = text.lower()
    return TaskSignals(
        complex_planning=any(word in normalized for word in ("plan", "design", "architecture", "refactor")),
        debugging=any(word in normalized for word in ("debug", "bug", "traceback", "regression")),
        review=any(word in normalized for word in ("review", "audit", "inspect")),
        multi_file_refactor=any(word in normalized for word in ("multi-file", "across files", "repository-wide")),
    )


def summarize_features(request: RoutingRequest) -> FeatureSummary:
    """Convert routing features to a bounded persistence representation."""

    signal_count = sum(
        (
            request.task_signals.complex_planning,
            request.task_signals.debugging,
            request.task_signals.review,
            request.task_signals.multi_file_refactor,
        )
    )
    return FeatureSummary(
        required_capabilities=tuple(sorted(cap.value for cap in request.required_capabilities)),
        input_size_bucket=bucket(request.estimated_input_tokens, (8000, 64000, 200000)),
        message_count_bucket=bucket(request.message_count, (4, 12, 40)),
        tool_rounds_bucket=bucket(request.tool_rounds, (0, 2, 5)),
        outcome_signal=request.outcome_signal.value,
        task_signal_count=signal_count,
    )
