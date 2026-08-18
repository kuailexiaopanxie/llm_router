"""Execution tests for health leases, outcomes, and commit semantics."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from conftest import ExchangeSpec, FakeProviderPort, envelope, router_config_data

from llm_router.config import HealthConfig, RouterConfig
from llm_router.domain import Protocol, ProxyResponse, RoutingRequest
from llm_router.execution.engine import ExecutionEngine
from llm_router.execution.stream_semantics import (
    AnthropicStreamSemantics,
    OpenAIStreamSemantics,
)
from llm_router.gateway.errors import RouterError
from llm_router.health.coordinator import InMemoryHealthCoordinator
from llm_router.health.models import AttemptOutcome, FailureClass
from llm_router.providers.port import ProviderFailure
from llm_router.routing.features import extract_routing_request
from llm_router.routing.kernel import RoutingKernel
from llm_router.routing.policy import compile_routing_policy


def _kernel(config: RouterConfig) -> RoutingKernel:
    """Build a routing kernel without prior session state."""

    return RoutingKernel(compile_routing_policy(config))


def _engine(
    config: RouterConfig,
    provider: FakeProviderPort,
    health: InMemoryHealthCoordinator,
) -> ExecutionEngine:
    """Build a health-aware engine over one deterministic fake provider."""

    return ExecutionEngine(
        {name: provider for name in config.providers},
        {
            Protocol.ANTHROPIC_MESSAGES: AnthropicStreamSemantics(),
            Protocol.OPENAI_RESPONSES: OpenAIStreamSemantics(),
        },
        health,
        config.health.max_cooldown_seconds,
    )


def _request() -> RoutingRequest:
    """Build a minimal Anthropic routing request."""

    return extract_routing_request(
        {"messages": [{"role": "user", "content": "hello"}]},
        "code/fast",
    )


def test_health_skip_does_not_increment_upstream_attempt_count(
    router_config: RouterConfig,
) -> None:
    """Skip a stale primary lease and count only the fallback invocation."""

    now = datetime.now(timezone.utc)
    targets = router_config.model_targets()
    health = InMemoryHealthCoordinator(HealthConfig(), targets)
    request = _request()
    plan = _kernel(router_config).plan(request, health.snapshot(now))
    lease = health.acquire(plan.primary, now)
    assert lease is not None
    health.record(lease, AttemptOutcome(FailureClass.TARGET_PERMANENT, now))
    provider = FakeProviderPort(
        {plan.fallbacks[0].alias: [ExchangeSpec(chunks=(b'{"ok":true}',))]}
    )

    response = asyncio.run(
        _engine(router_config, provider, health).execute(
            envelope(Protocol.ANTHROPIC_MESSAGES, {"messages": []}),
            plan,
        )
    )

    assert response.attempt_count == 1
    assert response.health_skipped_count == 1
    assert [attempt.status for attempt in response.attempts] == ["health_skipped", "success"]
    assert len(provider.invoke_calls) == 1


@pytest.mark.parametrize(
    ("protocol", "terminal"),
    [
        (
            Protocol.ANTHROPIC_MESSAGES,
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        ),
        (
            Protocol.OPENAI_RESPONSES,
            b'event: response.completed\ndata: {"type":"response.completed","response":{}}\n\n',
        ),
    ],
)
def test_sse_requires_protocol_terminal_event_for_success(
    router_config: RouterConfig,
    protocol: Protocol,
    terminal: bytes,
) -> None:
    """Record stream success only after the protocol completion event."""

    now = datetime.now(timezone.utc)
    health = InMemoryHealthCoordinator(router_config.health, router_config.model_targets())
    if protocol is Protocol.ANTHROPIC_MESSAGES:
        body = {"messages": [{"role": "user", "content": "hello"}], "stream": True}
        request = extract_routing_request(body, "code/fast")
    else:
        from llm_router.routing.openai_features import (
            extract_routing_request as openai_request,
        )

        body = {"input": "hello", "stream": True}
        request = openai_request(body, "code/fast")
    plan = _kernel(router_config).plan(request, health.snapshot(now))
    first = b'event: start\ndata: {"type":"start"}\n\n'
    provider = FakeProviderPort(
        {plan.primary.alias: [ExchangeSpec(200, "text/event-stream", (first, terminal))]}
    )

    async def execute() -> tuple[ProxyResponse, bytes]:
        """Execute and consume one complete stream."""

        response = await _engine(router_config, provider, health).execute(
            envelope(protocol, body, stream=True),
            plan,
        )
        assert not isinstance(response.body, bytes)
        payload = b"".join([chunk async for chunk in response.body])
        await response.completion
        return response, payload

    response, payload = asyncio.run(execute())

    assert terminal in payload
    assert response.completion.result().status == "success"
    assert health.snapshot(datetime.now(timezone.utc)).target_states[plan.primary.alias].eligible


def test_incomplete_committed_stream_fails_without_fallback(
    router_config: RouterConfig,
) -> None:
    """Penalize an incomplete committed stream without invoking fallback."""

    now = datetime.now(timezone.utc)
    health = InMemoryHealthCoordinator(
        HealthConfig(failure_threshold=1),
        router_config.model_targets(),
    )
    body = {"messages": [{"role": "user", "content": "hello"}], "stream": True}
    request = extract_routing_request(body, "code/fast")
    plan = _kernel(router_config).plan(request, health.snapshot(now))
    first = b'event: message_start\ndata: {"type":"message_start"}\n\n'
    provider = FakeProviderPort(
        {
            plan.primary.alias: [ExchangeSpec(200, "text/event-stream", (first,))],
            plan.fallbacks[0].alias: [ExchangeSpec(200, "text/event-stream", (first,))],
        }
    )

    async def execute() -> bytes:
        """Consume the stream through its post-commit error event."""

        response = await _engine(router_config, provider, health).execute(
            envelope(Protocol.ANTHROPIC_MESSAGES, body, stream=True),
            plan,
        )
        assert not isinstance(response.body, bytes)
        payload = b"".join([chunk async for chunk in response.body])
        await response.completion
        return payload

    payload = asyncio.run(execute())

    assert b"event: error" in payload
    assert len(provider.invoke_calls) == 1
    assert not health.snapshot(datetime.now(timezone.utc)).target_states[plan.primary.alias].eligible


def test_all_stale_leases_return_no_available_target(
    router_config: RouterConfig,
) -> None:
    """Return 503 without an upstream call when every planned lease is stale."""

    now = datetime.now(timezone.utc)
    health = InMemoryHealthCoordinator(router_config.health, router_config.model_targets())
    request = _request()
    plan = _kernel(router_config).plan(request, health.snapshot(now))
    for target in plan.targets:
        lease = health.acquire(target, now)
        assert lease is not None
        health.record(lease, AttemptOutcome(FailureClass.TARGET_PERMANENT, now))
    provider = FakeProviderPort({})

    with pytest.raises(RouterError) as captured:
        asyncio.run(
            _engine(router_config, provider, health).execute(
                envelope(Protocol.ANTHROPIC_MESSAGES, {"messages": []}),
                plan,
            )
        )

    assert captured.value.code == "router_no_available_target"
    assert captured.value.health_skipped_count == len(plan.targets)
    assert provider.invoke_calls == []


def test_two_provider_503_failures_make_third_request_bypass_primary() -> None:
    """Stop paying primary failure latency after the configured threshold."""

    raw = router_config_data()
    raw["providers"]["anthropic_backup"] = {
        "type": "anthropic",
        "base_url": "https://anthropic-backup.invalid",
        "api_key_env": "ANTHROPIC_BACKUP_TEST_KEY",
    }
    raw["models"]["anthropic_balanced"]["provider"] = "anthropic_backup"
    raw["models"]["anthropic_deep"]["provider"] = "anthropic_backup"
    config = RouterConfig.model_validate(raw)
    health = InMemoryHealthCoordinator(config.health, config.model_targets())
    request = _request()
    provider = FakeProviderPort(
        {
            "anthropic_fast": [ExchangeSpec(status_code=503), ExchangeSpec(status_code=503)],
            "anthropic_balanced": [
                ExchangeSpec(chunks=(b'{"ok":true}',)),
                ExchangeSpec(chunks=(b'{"ok":true}',)),
                ExchangeSpec(chunks=(b'{"ok":true}',)),
            ],
        }
    )

    for _ in range(2):
        now = datetime.now(timezone.utc)
        plan = _kernel(config).plan(request, health.snapshot(now))
        response = asyncio.run(
            _engine(config, provider, health).execute(
                envelope(Protocol.ANTHROPIC_MESSAGES, {"messages": []}),
                plan,
            )
        )
        assert response.attempt_count == 2

    third_plan = _kernel(config).plan(
        request,
        health.snapshot(datetime.now(timezone.utc)),
    )
    third_response = asyncio.run(
        _engine(config, provider, health).execute(
            envelope(Protocol.ANTHROPIC_MESSAGES, {"messages": []}),
            third_plan,
        )
    )

    assert third_plan.primary.alias == "anthropic_balanced"
    assert third_response.attempt_count == 1
    assert [call.target.alias for call in provider.invoke_calls] == [
        "anthropic_fast",
        "anthropic_balanced",
        "anthropic_fast",
        "anthropic_balanced",
        "anthropic_balanced",
    ]


def test_transport_failure_is_bounded_and_allows_precommit_fallback(
    router_config: RouterConfig,
) -> None:
    """Classify adapter transport failure without exposing exception text."""

    health = InMemoryHealthCoordinator(
        HealthConfig(failure_threshold=2),
        router_config.model_targets(),
    )
    request = _request()
    plan = _kernel(router_config).plan(
        request,
        health.snapshot(datetime.now(timezone.utc)),
    )
    provider = FakeProviderPort(
        {
            plan.primary.alias: [
                ExchangeSpec(
                    invoke_failure=ProviderFailure(
                        "router_upstream_connect_failed",
                        FailureClass.PROVIDER_TRANSIENT,
                    )
                )
            ],
            plan.fallbacks[0].alias: [ExchangeSpec(chunks=(b'{"ok":true}',))],
        }
    )

    response = asyncio.run(
        _engine(router_config, provider, health).execute(
            envelope(Protocol.ANTHROPIC_MESSAGES, {"messages": []}),
            plan,
        )
    )

    assert response.attempt_count == 2
    assert response.attempts[0].error_code == "router_upstream_retryable"


def test_client_stream_close_is_health_neutral(router_config: RouterConfig) -> None:
    """Release a committed stream lease without penalizing its Provider."""

    health = InMemoryHealthCoordinator(
        HealthConfig(failure_threshold=1),
        router_config.model_targets(),
    )
    body = {"messages": [{"role": "user", "content": "hello"}], "stream": True}
    request = extract_routing_request(body, "code/fast")
    plan = _kernel(router_config).plan(
        request,
        health.snapshot(datetime.now(timezone.utc)),
    )
    first = b'event: message_start\ndata: {"type":"message_start"}\n\n'
    provider = FakeProviderPort(
        {plan.primary.alias: [ExchangeSpec(200, "text/event-stream", (first,))]}
    )

    async def execute_and_close() -> ProxyResponse:
        """Commit one event and explicitly close the downstream iterator."""

        response = await _engine(router_config, provider, health).execute(
            envelope(Protocol.ANTHROPIC_MESSAGES, body, stream=True),
            plan,
        )
        assert not isinstance(response.body, bytes)
        assert await response.body.__anext__() == first
        await response.body.aclose()  # type: ignore[attr-defined]
        await response.completion
        return response

    response = asyncio.run(execute_and_close())

    assert response.completion.result().status == "cancelled"
    assert health.snapshot(datetime.now(timezone.utc)).target_states[plan.primary.alias].eligible
