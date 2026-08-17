"""Extract bounded, non-sensitive routing features from OpenAI Responses JSON."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from llm_router.domain import Capability, OutcomeSignal, Protocol, RoutingRequest
from llm_router.routing.feature_utils import bucket, classify_task_text


def _mappings(value: Any) -> Iterator[Mapping[str, Any]]:
    """Iterate nested request mappings without retaining their values."""

    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            yield current
            stack.extend(current.values())
        elif isinstance(current, Sequence) and not isinstance(current, (str, bytes, bytearray)):
            stack.extend(current)


def _text_fragments(value: Any) -> list[str]:
    """Collect input text only for ephemeral generic task classification."""

    if isinstance(value, str):
        return [value]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    fragments: list[str] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        item_type = item.get("type")
        if item_type in {"input_text", "message"}:
            text = item.get("text")
            if isinstance(text, str):
                fragments.append(text)
            fragments.extend(_text_fragments(item.get("content")))
    return fragments


def _outcome_signal(input_value: Any) -> OutcomeSignal:
    """Extract explicit function-output success or failure facts."""

    found_output = False
    for item in _mappings(input_value):
        if item.get("type") != "function_call_output":
            continue
        status = item.get("status")
        if item.get("is_error") is True or isinstance(status, str) and status in {
            "failed",
            "error",
            "incomplete",
        }:
            return OutcomeSignal.FAILURE
        found_output = True
    return OutcomeSignal.SUCCESS if found_output else OutcomeSignal.UNKNOWN


def extract_routing_request(
    body: Mapping[str, Any], requested_profile: str, session_id: str | None
) -> RoutingRequest:
    """Build a routing request from a validated-but-preserved Responses body."""

    input_value = body.get("input", "")
    input_items = (
        len(input_value)
        if isinstance(input_value, Sequence) and not isinstance(input_value, (str, bytes, bytearray))
        else 1
    )
    input_mappings = list(_mappings(input_value))
    tools_value = body.get("tools", [])
    tools = (
        [item for item in tools_value if isinstance(item, Mapping)]
        if isinstance(tools_value, Sequence) and not isinstance(tools_value, (str, bytes, bytearray))
        else []
    )
    provider_managed_tools = any(tool.get("type") != "function" for tool in tools)
    tool_choice = body.get("tool_choice")
    reasoning_requested = isinstance(body.get("reasoning"), Mapping)
    text_config = body.get("text")
    structured_output = isinstance(text_config, Mapping) and isinstance(text_config.get("format"), Mapping)
    response_state_requested = bool(body.get("previous_response_id")) or body.get("conversation") is not None
    has_vision = any(
        item.get("type") == "input_image" or "image_url" in item for item in input_mappings
    )
    tool_rounds = sum(item.get("type") == "function_call" for item in input_mappings)
    stream = body.get("stream") is True

    required: set[Capability] = set()
    if stream:
        required.add(Capability.STREAMING)
    if tools or tool_choice is not None and tool_choice != "none":
        required.add(Capability.TOOLS)
    if reasoning_requested:
        required.add(Capability.REASONING)
    if structured_output:
        required.add(Capability.STRUCTURED_OUTPUT)
    if has_vision:
        required.add(Capability.VISION)
    if response_state_requested:
        required.add(Capability.RESPONSE_STATE)
    if provider_managed_tools:
        required.add(Capability.PROVIDER_MANAGED_TOOLS)

    instructions = body.get("instructions")
    text = "\n".join(
        _text_fragments(input_value) + ([instructions] if isinstance(instructions, str) else [])
    )
    estimated_tokens = max(1, len(str(body).encode("utf-8")) // 4)
    return RoutingRequest(
        requested_profile=requested_profile,
        required_capabilities=frozenset(required),
        estimated_input_tokens=estimated_tokens,
        message_count=input_items,
        tool_rounds=tool_rounds,
        system_size_bucket=bucket(len(instructions) if isinstance(instructions, str) else 0, (4000, 16000, 64000)),
        task_signals=classify_task_text(text),
        outcome_signal=_outcome_signal(input_value),
        session_id=session_id,
        stream=stream,
        protocol=Protocol.OPENAI_RESPONSES,
        response_state_requested=response_state_requested,
        provider_managed_tools_requested=provider_managed_tools,
    )
