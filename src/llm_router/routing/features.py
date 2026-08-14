"""Extract bounded, non-sensitive routing features from Anthropic JSON."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from llm_router.domain import Capability, FeatureSummary, OutcomeSignal, RoutingRequest, TaskSignals


def _bucket(value: int, limits: tuple[int, ...]) -> str:
    """Map a count to a stable bounded range label."""

    for limit in limits:
        if value <= limit:
            return f"le_{limit}"
    return f"gt_{limits[-1]}"


def _text_blocks(value: Any) -> list[str]:
    """Collect text values only for ephemeral keyword classification."""

    if isinstance(value, str):
        return [value]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        result: list[str] = []
        for item in value:
            if isinstance(item, Mapping):
                result.extend(_text_blocks(item.get("text")))
                result.extend(_text_blocks(item.get("content")))
        return result
    return []


def _outcome_signal(messages: Sequence[Any]) -> OutcomeSignal:
    """Extract explicit tool failure/success signals without guessing from prose."""

    found_success = False
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        content = message.get("content", [])
        if not isinstance(content, Sequence) or isinstance(content, (str, bytes, bytearray)):
            continue
        for block in content:
            if not isinstance(block, Mapping) or block.get("type") != "tool_result":
                continue
            if block.get("is_error") is True:
                return OutcomeSignal.FAILURE
            found_success = True
    return OutcomeSignal.SUCCESS if found_success else OutcomeSignal.UNKNOWN


def extract_routing_request(
    body: Mapping[str, Any], requested_profile: str, session_id: str | None, count_only: bool = False
) -> RoutingRequest:
    """Build a routing request from a validated-but-preserved Anthropic body."""

    messages = body.get("messages", [])
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes, bytearray)):
        messages = []
    stream = body.get("stream") is True
    tools = body.get("tools")
    has_tools = isinstance(tools, Sequence) and not isinstance(tools, (str, bytes, bytearray)) and bool(tools)
    has_tool_choice = body.get("tool_choice") is not None
    has_thinking = isinstance(body.get("thinking"), Mapping) and body["thinking"].get("type") == "enabled"
    has_vision = False
    tool_rounds = 0
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        role = message.get("role")
        content = message.get("content", [])
        if role == "assistant" and isinstance(content, Sequence) and not isinstance(content, str):
            if any(isinstance(block, Mapping) and block.get("type") == "tool_use" for block in content):
                tool_rounds += 1
        if isinstance(content, Sequence) and not isinstance(content, (str, bytes, bytearray)):
            has_vision = has_vision or any(
                isinstance(block, Mapping)
                and block.get("type") == "image"
                for block in content
            )
    system = body.get("system", [])
    system_text = "\n".join(_text_blocks(system))
    all_text = "\n".join(_text_blocks(messages) + [system_text]).lower()
    task_signals = TaskSignals(
        complex_planning=any(word in all_text for word in ("plan", "design", "architecture", "refactor")),
        debugging=any(word in all_text for word in ("debug", "bug", "traceback", "regression")),
        review=any(word in all_text for word in ("review", "audit", "inspect")),
        multi_file_refactor=any(word in all_text for word in ("multi-file", "across files", "repository-wide")),
    )
    required: set[Capability] = set()
    if stream:
        required.add(Capability.STREAMING)
    if has_tools or has_tool_choice:
        required.add(Capability.TOOLS)
    if has_thinking:
        required.add(Capability.THINKING)
    if has_vision:
        required.add(Capability.VISION)
    has_cache_control = any(
        isinstance(item, Mapping) and "cache_control" in item
        for item in (tools or [])
    ) or "cache_control" in str(body.get("system", ""))
    if has_cache_control:
        required.add(Capability.PROMPT_CACHE)
    estimated_tokens = max(1, len(str(body).encode("utf-8")) // 4)
    return RoutingRequest(
        requested_profile=requested_profile,
        required_capabilities=frozenset(required),
        estimated_input_tokens=estimated_tokens,
        message_count=len(messages),
        tool_rounds=tool_rounds,
        system_size_bucket=_bucket(len(system_text), (4000, 16000, 64000)),
        task_signals=task_signals,
        outcome_signal=_outcome_signal(messages),
        session_id=session_id,
        stream=stream,
        count_only=count_only,
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
        input_size_bucket=_bucket(request.estimated_input_tokens, (8000, 64000, 200000)),
        message_count_bucket=_bucket(request.message_count, (4, 12, 40)),
        tool_rounds_bucket=_bucket(request.tool_rounds, (0, 2, 5)),
        outcome_signal=request.outcome_signal.value,
        task_signal_count=signal_count,
    )
