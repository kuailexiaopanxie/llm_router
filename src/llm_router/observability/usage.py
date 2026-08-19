"""Pure protocol usage normalization without retaining Provider payloads."""

from __future__ import annotations

from collections.abc import Mapping

from llm_router.observability.models import UsageBreakdown, UsageStatus


def _token(value: object) -> int | None:
    """Return a non-negative integer token value or reject its type."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("provider usage token is invalid")
    return value


def normalize_anthropic_usage(payload: Mapping[str, object]) -> UsageBreakdown:
    """Normalize one Anthropic terminal usage payload."""

    usage_value = payload.get("usage")
    if not isinstance(usage_value, Mapping):
        return UsageBreakdown.missing()
    fields = {
        "input_uncached_tokens": "input_tokens",
        "input_cache_read_tokens": "cache_read_input_tokens",
        "input_cache_write_tokens": "cache_creation_input_tokens",
        "output_tokens": "output_tokens",
    }
    values: dict[str, int | None] = {}
    try:
        for target, source in fields.items():
            values[target] = _token(usage_value[source]) if source in usage_value else None
    except ValueError:
        return UsageBreakdown(UsageStatus.INVALID)
    present = sum(value is not None for value in values.values())
    status = (
        UsageStatus.COMPLETE
        if values["input_uncached_tokens"] is not None
        and values["output_tokens"] is not None
        else UsageStatus.PARTIAL
    )
    if present == 0:
        return UsageBreakdown.missing()
    return UsageBreakdown(status=status, **values)


def normalize_openai_usage(payload: Mapping[str, object]) -> UsageBreakdown:
    """Normalize one OpenAI Responses terminal usage payload."""

    usage_value = payload.get("usage")
    if not isinstance(usage_value, Mapping):
        return UsageBreakdown.missing()
    input_details = usage_value.get("input_tokens_details")
    output_details = usage_value.get("output_tokens_details")
    try:
        total_input = _token(usage_value["input_tokens"]) if "input_tokens" in usage_value else None
        cached = (
            _token(input_details["cached_tokens"])
            if isinstance(input_details, Mapping) and "cached_tokens" in input_details
            else None
        )
        output = _token(usage_value["output_tokens"]) if "output_tokens" in usage_value else None
        reasoning = (
            _token(output_details["reasoning_tokens"])
            if isinstance(output_details, Mapping) and "reasoning_tokens" in output_details
            else None
        )
        if total_input is not None and cached is not None and cached > total_input:
            raise ValueError("cached input exceeds total input")
        if output is not None and reasoning is not None and reasoning > output:
            raise ValueError("reasoning output exceeds total output")
    except ValueError:
        return UsageBreakdown(UsageStatus.INVALID)
    uncached = total_input - cached if total_input is not None and cached is not None else total_input
    values = (uncached, cached, output, reasoning)
    if all(value is None for value in values):
        return UsageBreakdown.missing()
    status = UsageStatus.COMPLETE if total_input is not None and output is not None else UsageStatus.PARTIAL
    return UsageBreakdown(status, uncached, cached, None, output, reasoning)


def merge_usage(current: UsageBreakdown, fragment: UsageBreakdown) -> UsageBreakdown:
    """Merge later terminal SSE counters over earlier known categories."""

    if fragment.status is UsageStatus.INVALID:
        return fragment
    values = tuple(
        new if new is not None else old
        for old, new in zip(
            (
                current.input_uncached_tokens,
                current.input_cache_read_tokens,
                current.input_cache_write_tokens,
                current.output_tokens,
                current.reasoning_output_tokens,
            ),
            (
                fragment.input_uncached_tokens,
                fragment.input_cache_read_tokens,
                fragment.input_cache_write_tokens,
                fragment.output_tokens,
                fragment.reasoning_output_tokens,
            ),
        )
    )
    known = sum(value is not None for value in values)
    status = UsageStatus.PARTIAL if known else UsageStatus.MISSING
    if UsageStatus.COMPLETE in {current.status, fragment.status}:
        status = UsageStatus.COMPLETE
    return UsageBreakdown(status, *values)
