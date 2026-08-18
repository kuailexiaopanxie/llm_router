"""Offline regression fixtures freezing v0.2 routing and proxy behavior."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest
from conftest import ExchangeSpec, FakeProviderPort, envelope, router_config_data

from llm_router.config import RouterConfig
from llm_router.domain import ExecutionPlan, Protocol, ProxyResponse, RoutingRequest
from llm_router.execution.engine import ExecutionEngine
from llm_router.execution.stream_semantics import (
    AnthropicStreamSemantics,
    OpenAIStreamSemantics,
)
from llm_router.health.coordinator import DisabledHealthCoordinator
from llm_router.routing.features import extract_routing_request as anthropic_features
from llm_router.routing.kernel import RoutingKernel
from llm_router.routing.openai_features import (
    extract_routing_request as openai_features,
)
from llm_router.routing.policy import compile_routing_policy


def _kernel(config: RouterConfig) -> RoutingKernel:
    """Build a stateless routing kernel for one test."""

    return RoutingKernel(compile_routing_policy(config))


def _health(config: RouterConfig) -> DisabledHealthCoordinator:
    """Build the all-healthy adapter used to freeze v0.2 behavior."""

    return DisabledHealthCoordinator(config.model_targets())


def _plan(
    kernel: RoutingKernel,
    config: RouterConfig,
    request: RoutingRequest,
) -> ExecutionPlan:
    """Plan one request against an explicit all-healthy snapshot."""

    return kernel.plan(request, _health(config).snapshot(datetime.now(timezone.utc)))


def _engine(provider: FakeProviderPort, config: RouterConfig) -> ExecutionEngine:
    """Build an engine with one fake port for both protocol providers."""

    return ExecutionEngine(
        {"anthropic": provider, "openai": provider},
        {
            Protocol.ANTHROPIC_MESSAGES: AnthropicStreamSemantics(),
            Protocol.OPENAI_RESPONSES: OpenAIStreamSemantics(),
        },
        _health(config),
        config.health.max_cooldown_seconds,
    )


def test_protocol_hard_filter_and_capability_equivalent_fallback(router_config: RouterConfig) -> None:
    """Keep every planned target on the inbound protocol."""

    kernel = _kernel(router_config)
    anthropic = anthropic_features(
        {"messages": [{"role": "user", "content": "hello"}]}, "code/auto"
    )
    openai = openai_features({"input": "hello"}, "code/auto")

    anthropic_plan = _plan(kernel, router_config, anthropic)
    openai_plan = _plan(kernel, router_config, openai)

    assert all(target.protocol is Protocol.ANTHROPIC_MESSAGES for target in anthropic_plan.targets)
    assert all(target.protocol is Protocol.OPENAI_RESPONSES for target in openai_plan.targets)
    assert anthropic_plan.primary.alias == "anthropic_fast"
    assert openai_plan.primary.alias == "openai_fast"


def test_response_state_does_not_cross_state_scope() -> None:
    """Filter an otherwise capable stateful fallback in another state scope."""

    raw = router_config_data()
    raw["profiles"] = {"code/auto": {"mode": "auto"}}
    raw["models"]["openai_deep"]["state_scope"] = "openai-other"
    config = RouterConfig.model_validate(raw)
    request = openai_features(
        {"input": "continue", "previous_response_id": "resp_private"}, "code/auto"
    )

    plan = _plan(_kernel(config), config, request)

    assert [target.alias for target in plan.targets] == ["openai_balanced"]
    assert "state_scope_filtered" in plan.auxiliary_reasons


@pytest.mark.parametrize(
    ("protocol", "request_body", "success_body"),
    [
        (
            Protocol.ANTHROPIC_MESSAGES,
            {"messages": [{"role": "user", "content": "hello"}]},
            b'{"id":"msg_kept","unknown":{"kept":true}}',
        ),
        (
            Protocol.OPENAI_RESPONSES,
            {"input": "hello"},
            b'{"id":"resp_kept","unknown":{"kept":true}}',
        ),
    ],
)
def test_json_retryable_fallback_and_unknown_field_passthrough(
    router_config: RouterConfig,
    protocol: Protocol,
    request_body: dict[str, object],
    success_body: bytes,
) -> None:
    """Fallback before commit and preserve successful JSON bytes."""

    request = (
        anthropic_features(request_body, "code/auto")
        if protocol is Protocol.ANTHROPIC_MESSAGES
        else openai_features(request_body, "code/auto")
    )
    plan = _plan(_kernel(router_config), router_config, request)
    provider = FakeProviderPort(
        {
            plan.primary.alias: [ExchangeSpec(status_code=503)],
            plan.fallbacks[0].alias: [ExchangeSpec(chunks=(success_body,))],
        }
    )

    response = asyncio.run(
        _engine(provider, router_config).execute(envelope(protocol, request_body), plan)
    )

    assert isinstance(response.body, bytes)
    assert response.body == success_body
    assert json.loads(response.body)["unknown"] == {"kept": True}
    assert response.attempt_count == 2
    assert len(provider.invoke_calls) == 2


@pytest.mark.parametrize("protocol", list(Protocol))
def test_sse_commit_point_preserves_unknown_event_without_fallback(
    router_config: RouterConfig, protocol: Protocol
) -> None:
    """Relay unknown SSE events and render failures after commit without fallback."""

    if protocol is Protocol.ANTHROPIC_MESSAGES:
        body = {"messages": [{"role": "user", "content": "hello"}], "stream": True}
        request = anthropic_features(body, "code/auto")
        first = b'event: future_event\ndata: {"type":"future_event","unknown":true}\n\n'
    else:
        body = {"input": "hello", "stream": True}
        request = openai_features(body, "code/auto")
        first = b'event: response.future\ndata: {"type":"response.future","unknown":true}\n\n'
    plan = _plan(_kernel(router_config), router_config, request)
    provider = FakeProviderPort(
        {
            plan.primary.alias: [ExchangeSpec(200, "text/event-stream", (first,), OSError("private"))],
            plan.fallbacks[0].alias: [ExchangeSpec(content_type="text/event-stream")],
        }
    )

    async def execute_and_read() -> tuple[ProxyResponse, bytes]:
        """Execute and fully consume the committed stream."""

        response = await _engine(provider, router_config).execute(
            envelope(protocol, body, stream=True), plan
        )
        assert not isinstance(response.body, bytes)
        payload = b"".join([chunk async for chunk in response.body])
        return response, payload

    response, payload = asyncio.run(execute_and_read())

    assert first in payload
    assert b"event: error" in payload
    assert len(provider.invoke_calls) == 1
    assert response.attempt_count == 1
