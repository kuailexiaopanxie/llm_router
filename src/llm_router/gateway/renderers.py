"""Protocol-specific rendering for sanitized router errors."""

from __future__ import annotations

import json
import math
from typing import Protocol

from fastapi.responses import JSONResponse

from llm_router.errors import RouterError


def _error_headers(error: RouterError, request_id: str) -> dict[str, str]:
    """Build safe router error headers with bounded Retry-After."""

    headers = {"x-llm-router-request-id": request_id}
    if error.retry_after is not None:
        headers["retry-after"] = str(max(0, math.ceil(error.retry_after)))
    return headers


class ErrorRenderer(Protocol):
    """Render one protocol-compatible router error."""

    def json_error(self, error: RouterError, request_id: str) -> JSONResponse:
        """Render a non-stream error response."""

    def stream_error(self, error: RouterError) -> bytes:
        """Render a post-commit SSE error event."""


def _anthropic_error_type(error: RouterError) -> str:
    """Map an internal router error to a bounded Anthropic error category."""

    if error.http_status == 401:
        return "authentication_error"
    if error.http_status in {400, 422}:
        return "invalid_request_error"
    if error.http_status in {429, 503}:
        return "overloaded_error"
    return "api_error"


def _openai_error_type(error: RouterError) -> str:
    """Map an internal router error to a bounded OpenAI error category."""

    if error.http_status == 401:
        return "authentication_error"
    if error.http_status in {400, 422}:
        return "invalid_request_error"
    if error.http_status == 429:
        return "rate_limit_error"
    return "api_error"


class AnthropicErrorRenderer:
    """Render Anthropic Messages compatible error bodies and events."""

    def json_error(self, error: RouterError, request_id: str) -> JSONResponse:
        """Render a normalized Anthropic JSON error."""

        return JSONResponse(
            status_code=error.http_status,
            headers=_error_headers(error, request_id),
            content={
                "type": "error",
                "error": {"type": _anthropic_error_type(error), "message": error.safe_message},
                "request_id": request_id,
            },
        )

    def stream_error(self, error: RouterError) -> bytes:
        """Render a normalized Anthropic SSE error event."""

        payload = {
            "type": "error",
            "error": {"type": _anthropic_error_type(error), "message": error.safe_message},
        }
        return b"event: error\ndata: " + json.dumps(payload, separators=(",", ":")).encode() + b"\n\n"


class OpenAIErrorRenderer:
    """Render OpenAI Responses compatible error bodies and events."""

    def json_error(self, error: RouterError, request_id: str) -> JSONResponse:
        """Render a normalized OpenAI JSON error."""

        return JSONResponse(
            status_code=error.http_status,
            headers=_error_headers(error, request_id),
            content={
                "error": {
                    "message": error.safe_message,
                    "type": _openai_error_type(error),
                    "param": None,
                    "code": error.code,
                },
                "request_id": request_id,
            },
        )

    def stream_error(self, error: RouterError) -> bytes:
        """Render a normalized OpenAI Responses SSE error event."""

        payload = {
            "type": "error",
            "code": error.code,
            "message": error.safe_message,
            "param": None,
        }
        return b"event: error\ndata: " + json.dumps(payload, separators=(",", ":")).encode() + b"\n\n"
