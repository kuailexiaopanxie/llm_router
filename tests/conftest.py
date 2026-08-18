"""Shared offline fixtures for router regression and reliability tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pytest

from llm_router.config import RouterConfig
from llm_router.domain import Protocol, ProtocolEnvelope, ProviderExchange, ProviderRequest


@dataclass(frozen=True, slots=True)
class ExchangeSpec:
    """Describe one deterministic fake upstream exchange."""

    status_code: int = 200
    content_type: str = "application/json"
    chunks: tuple[bytes, ...] = (b"{}",)
    failure: Exception | None = None
    invoke_failure: Exception | None = None
    response_headers: tuple[tuple[str, str], ...] = ()


class FakeProviderPort:
    """Return queued exchanges while recording actual upstream invocations."""

    def __init__(self, exchanges: dict[str, list[ExchangeSpec]]) -> None:
        """Initialize target-specific exchange queues."""

        self._exchanges = exchanges
        self.invoke_calls: list[ProviderRequest] = []
        self.close_count = 0

    async def invoke(self, request: ProviderRequest) -> ProviderExchange:
        """Open the next queued exchange for the resolved target."""

        self.invoke_calls.append(request)
        spec = self._exchanges[request.target.alias].pop(0)
        if spec.invoke_failure is not None:
            raise spec.invoke_failure
        closed = False

        async def body() -> AsyncIterator[bytes]:
            """Yield fixed chunks and optionally fail after them."""

            for chunk in spec.chunks:
                yield chunk
            if spec.failure is not None:
                raise spec.failure

        async def close() -> None:
            """Record one idempotent exchange close."""

            nonlocal closed
            if not closed:
                closed = True
                self.close_count += 1

        headers = {"content-type": spec.content_type, **dict(spec.response_headers)}
        return ProviderExchange(spec.status_code, headers, body(), close)


def router_config_data() -> dict[str, Any]:
    """Return one dual-protocol configuration with deterministic aliases."""

    return {
        "version": 1,
        "server": {},
        "storage": {"sqlite_path": ":memory:"},
        "providers": {
            "anthropic": {
                "type": "anthropic",
                "base_url": "https://anthropic.invalid",
                "api_key_env": "ANTHROPIC_TEST_KEY",
            },
            "openai": {
                "type": "openai",
                "base_url": "https://openai.invalid",
                "api_key_env": "OPENAI_TEST_KEY",
                "auth_scheme": "bearer",
            },
        },
        "models": {
            "anthropic_fast": {
                "provider": "anthropic",
                "upstream_model": "anthropic-fast",
                "tier": "fast",
                "capabilities": ["streaming", "tools"],
                "max_input_tokens": 200_000,
            },
            "anthropic_balanced": {
                "provider": "anthropic",
                "upstream_model": "anthropic-balanced",
                "tier": "balanced",
                "capabilities": ["streaming", "tools", "thinking", "vision"],
                "max_input_tokens": 200_000,
            },
            "anthropic_deep": {
                "provider": "anthropic",
                "upstream_model": "anthropic-deep",
                "tier": "deep",
                "capabilities": ["streaming", "tools", "thinking", "vision"],
                "max_input_tokens": 200_000,
            },
            "openai_fast": {
                "provider": "openai",
                "upstream_model": "openai-fast",
                "protocol": "openai_responses",
                "state_scope": "openai-default",
                "tier": "fast",
                "capabilities": ["streaming", "tools", "reasoning", "structured_output"],
                "max_input_tokens": 200_000,
            },
            "openai_balanced": {
                "provider": "openai",
                "upstream_model": "openai-balanced",
                "protocol": "openai_responses",
                "state_scope": "openai-default",
                "tier": "balanced",
                "capabilities": [
                    "streaming",
                    "tools",
                    "reasoning",
                    "structured_output",
                    "response_state",
                ],
                "max_input_tokens": 200_000,
            },
            "openai_deep": {
                "provider": "openai",
                "upstream_model": "openai-deep",
                "protocol": "openai_responses",
                "state_scope": "openai-default",
                "tier": "deep",
                "capabilities": [
                    "streaming",
                    "tools",
                    "reasoning",
                    "structured_output",
                    "response_state",
                ],
                "max_input_tokens": 200_000,
            },
        },
        "profiles": {
            "code/auto": {"mode": "auto"},
            "code/fast": {
                "targets": {
                    "anthropic_messages": {
                        "primary": "anthropic_fast",
                        "fallback": ["anthropic_balanced"],
                    },
                    "openai_responses": {
                        "primary": "openai_fast",
                        "fallback": ["openai_balanced"],
                    },
                }
            },
        },
        "routing": {"default_profile": "code/auto", "attempt_limit": 2},
        "timeouts": {
            "connect_seconds": 1,
            "response_header_seconds": 1,
            "non_stream_deadline_seconds": 5,
            "stream_idle_seconds": 1,
            "stream_max_seconds": 5,
        },
    }


@pytest.fixture
def router_config() -> RouterConfig:
    """Build the validated dual-protocol test configuration."""

    return RouterConfig.model_validate(router_config_data())


def envelope(protocol: Protocol, body: dict[str, Any], stream: bool = False) -> ProtocolEnvelope:
    """Build a protocol envelope with fixed safe metadata."""

    endpoint = "/v1/responses" if protocol is Protocol.OPENAI_RESPONSES else "/v1/messages"
    return ProtocolEnvelope(
        request_id="test-request",
        protocol=protocol,
        raw_body=body,
        safe_headers={},
        stream=stream,
        received_at=datetime.now(timezone.utc),
        endpoint=endpoint,
    )
