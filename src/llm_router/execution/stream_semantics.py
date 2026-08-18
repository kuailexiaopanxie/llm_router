"""Protocol-specific validation and observation of SSE events."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Protocol

from llm_router.errors import RouterError
from llm_router.gateway.renderers import (
    AnthropicErrorRenderer,
    ErrorRenderer,
    OpenAIErrorRenderer,
)
from llm_router.health.models import FailureClass


class StreamSemantics(Protocol):
    """Validate and render protocol-native stream events."""

    def validate_first_event(self, event: bytes) -> None:
        """Reject an invalid first event before downstream commit."""

    def render_post_commit_error(self, code: str) -> bytes:
        """Render one safe protocol-native error event after commit."""

    def extract_usage(self, event: bytes) -> tuple[int | None, int | None]:
        """Extract bounded usage counters from one complete event."""

    def terminal_outcome(self, event: bytes) -> FailureClass | None:
        """Return a bounded outcome when an event terminates the stream."""


def _event_payload(event: bytes) -> Mapping[str, object]:
    """Parse JSON data lines from one complete SSE event."""

    data_lines = []
    for line in event.replace(b"\r\n", b"\n").split(b"\n"):
        if line.startswith(b"data:"):
            data_lines.append(line[5:].lstrip())
    if not data_lines:
        raise ValueError("SSE event has no data field")
    value = json.loads(b"\n".join(data_lines))
    if not isinstance(value, Mapping):
        raise TypeError("SSE data must be a JSON object")
    return value


class _JSONStreamSemantics:
    """Share structural SSE validation while preserving protocol renderers."""

    def __init__(self, renderer: ErrorRenderer) -> None:
        """Bind the renderer used for post-commit errors."""

        self._renderer = renderer

    def validate_first_event(self, event: bytes) -> None:
        """Require one JSON object data payload before stream commit."""

        try:
            _event_payload(event)
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RouterError(
                "router_upstream_invalid_stream",
                502,
                "The upstream returned an invalid event stream.",
                fallback_allowed=True,
            ) from exc

    def render_post_commit_error(self, code: str) -> bytes:
        """Render a fixed safe error without exposing upstream details."""

        return self._renderer.stream_error(
            RouterError(code, 502, "The upstream stream failed.")
        )


class AnthropicStreamSemantics(_JSONStreamSemantics):
    """Handle Anthropic Messages stream validation and usage."""

    def __init__(self) -> None:
        """Initialize Anthropic stream semantics."""

        super().__init__(AnthropicErrorRenderer())

    def extract_usage(self, event: bytes) -> tuple[int | None, int | None]:
        """Extract Anthropic input or output token counters when present."""

        try:
            payload = _event_payload(event)
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            return None, None
        message = payload.get("message")
        usage = message.get("usage") if isinstance(message, Mapping) else payload.get("usage")
        if not isinstance(usage, Mapping):
            return None, None
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        return (
            input_tokens if isinstance(input_tokens, int) else None,
            output_tokens if isinstance(output_tokens, int) else None,
        )

    def terminal_outcome(self, event: bytes) -> FailureClass | None:
        """Recognize Anthropic success and upstream error terminal events."""

        try:
            payload = _event_payload(event)
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        event_type = payload.get("type")
        if event_type == "message_stop":
            return FailureClass.SUCCESS
        if event_type == "error":
            return FailureClass.POST_COMMIT_STREAM_FAILURE
        return None


class OpenAIStreamSemantics(_JSONStreamSemantics):
    """Handle OpenAI Responses stream validation and usage."""

    def __init__(self) -> None:
        """Initialize OpenAI Responses stream semantics."""

        super().__init__(OpenAIErrorRenderer())

    def extract_usage(self, event: bytes) -> tuple[int | None, int | None]:
        """Extract usage only from a completed OpenAI response event."""

        try:
            payload = _event_payload(event)
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            return None, None
        if payload.get("type") != "response.completed":
            return None, None
        response = payload.get("response")
        usage = response.get("usage") if isinstance(response, Mapping) else None
        if not isinstance(usage, Mapping):
            return None, None
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        return (
            input_tokens if isinstance(input_tokens, int) else None,
            output_tokens if isinstance(output_tokens, int) else None,
        )

    def terminal_outcome(self, event: bytes) -> FailureClass | None:
        """Recognize OpenAI Responses success and failure terminal events."""

        try:
            payload = _event_payload(event)
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        event_type = payload.get("type")
        if event_type == "response.completed":
            return FailureClass.SUCCESS
        if event_type in {"error", "response.failed", "response.incomplete"}:
            return FailureClass.POST_COMMIT_STREAM_FAILURE
        return None
