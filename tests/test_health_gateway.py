"""Gateway behavior for protocol-specific no-available-target errors."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from fastapi import Request

from llm_router.config import HealthConfig, RouterConfig
from llm_router.domain import ExecutionStats, OutcomeSignal, Protocol, ProxyResponse
from llm_router.gateway.anthropic import AnthropicGateway
from llm_router.gateway.openai import OpenAIResponsesGateway
from llm_router.health.coordinator import InMemoryHealthCoordinator
from llm_router.health.models import AttemptOutcome, FailureClass
from llm_router.health.port import HealthPort
from llm_router.routing.kernel import RoutingKernel
from llm_router.routing.session import SessionStateStore

from conftest import envelope


def _request(path: str, body: dict[str, Any]) -> Request:
    """Build a Starlette request with a local client credential."""

    encoded = json.dumps(body).encode()
    sent = False

    async def receive() -> dict[str, object]:
        """Return the fixed request body once."""

        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": encoded, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": [
            (b"x-api-key", b"local-token"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(encoded)).encode()),
        ],
    }
    return Request(scope, receive)


def _runtime(
    config: RouterConfig,
    health: HealthPort,
    events: list[object],
    session_calls: list[tuple[object, ...]],
) -> SimpleNamespace:
    """Assemble the smallest runtime needed by either gateway."""

    async def execute(*_: object) -> None:
        """Fail the test if routing unexpectedly reaches execution."""

        raise AssertionError("execution must not start when no target is available")

    return SimpleNamespace(
        config=config,
        client_key="local-token",
        health=health,
        kernel=RoutingKernel(config, SessionStateStore(60, 100)),
        engine=SimpleNamespace(execute=execute),
        telemetry=SimpleNamespace(record=events.append),
        sessions=SimpleNamespace(record=lambda *items: session_calls.append(items)),
    )


def _unavailable_health(config: RouterConfig, provider: str) -> InMemoryHealthCoordinator:
    """Put one provider domain into cooldown before a gateway request."""

    targets = config.model_targets()
    health = InMemoryHealthCoordinator(HealthConfig(failure_threshold=1), targets)
    target = next(target for target in targets.values() if target.provider == provider)
    now = datetime.now(timezone.utc)
    lease = health.acquire(target, now)
    assert lease is not None
    health.record(lease, AttemptOutcome(FailureClass.PROVIDER_TRANSIENT, now))
    return health


def test_anthropic_no_available_target_has_retry_header_and_telemetry(
    router_config: RouterConfig,
) -> None:
    """Render Anthropic 503 and record a sanitized route failure."""

    config = RouterConfig.model_validate({**router_config.model_dump(), "health": {"failure_threshold": 1}})
    events: list[object] = []
    sessions: list[tuple[object, ...]] = []
    runtime = _runtime(config, _unavailable_health(config, "anthropic"), events, sessions)

    response = asyncio.run(
        AnthropicGateway(runtime).handle(
            _request(
                "/v1/messages",
                {"model": "code/auto", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]},
            )
        )
    )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "30"
    payload = json.loads(response.body)
    assert payload["error"]["type"] == "overloaded_error"
    assert len(events) == 1
    event = events[0]
    assert event.primary_model == "none"  # type: ignore[union-attr]
    assert event.attempt_count == 0  # type: ignore[union-attr]
    assert event.attempts == ()  # type: ignore[union-attr]
    assert event.health_filtered_count > 0  # type: ignore[union-attr]
    assert sessions == []


def test_openai_no_available_target_uses_openai_error_shape(
    router_config: RouterConfig,
) -> None:
    """Render OpenAI 503 while retaining same-protocol routing."""

    config = RouterConfig.model_validate({**router_config.model_dump(), "health": {"failure_threshold": 1}})
    events: list[object] = []
    sessions: list[tuple[object, ...]] = []
    runtime = _runtime(config, _unavailable_health(config, "openai"), events, sessions)

    response = asyncio.run(
        OpenAIResponsesGateway(runtime).handle(
            _request(
                "/v1/responses",
                {"model": "code/auto", "input": "hi", "max_output_tokens": 16},
            )
        )
    )

    assert response.status_code == 503
    payload = json.loads(response.body)
    assert payload["error"]["code"] == "router_no_available_target"
    assert payload["error"]["type"] == "api_error"
    assert len(events) == 1
    assert events[0].protocol == Protocol.OPENAI_RESPONSES.value  # type: ignore[union-attr]


def test_unknown_provider_exchange_does_not_update_session(router_config: RouterConfig) -> None:
    """Keep a successful Provider exchange distinct from task success."""

    from llm_router.gateway.common import record_completion
    from llm_router.health.coordinator import DisabledHealthCoordinator
    from llm_router.routing.features import extract_routing_request

    events: list[object] = []
    session_calls: list[tuple[object, ...]] = []
    health = DisabledHealthCoordinator(router_config.model_targets())
    runtime = _runtime(router_config, health, events, session_calls)
    body = {"messages": [{"role": "user", "content": "hi"}]}
    request = extract_routing_request(body, "code/fast", "session-1")
    now = datetime.now(timezone.utc)
    plan = runtime.kernel.plan(request, health.snapshot(now))

    async def record(outcome: OutcomeSignal) -> None:
        """Record one completed JSON response with a selected task outcome."""

        completion = asyncio.get_running_loop().create_future()
        completion.set_result(ExecutionStats("success", 1.0))
        response = ProxyResponse(
            status_code=200,
            headers={},
            body=b"{}",
            media_type="application/json",
            final_target=plan.primary,
            attempt_count=1,
            completion=completion,
        )
        await record_completion(
            runtime,
            envelope=envelope(Protocol.ANTHROPIC_MESSAGES, body),
            routing_request=replace(request, outcome_signal=outcome),
            plan=plan,
            response=response,
            usage_extractor=lambda _: (None, None),
        )

    asyncio.run(record(OutcomeSignal.UNKNOWN))
    asyncio.run(record(OutcomeSignal.SUCCESS))

    assert len(events) == 2
    assert session_calls == [("session-1", plan.primary.tier, OutcomeSignal.SUCCESS)]
