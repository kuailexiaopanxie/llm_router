"""v0.7 early Gateway lifecycle and trace response tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from conftest import router_config_data
from fastapi import Request
from fastapi.testclient import TestClient

from llm_router.app import create_app
from llm_router.config import RouterConfig
from llm_router.gateway.anthropic import AnthropicGateway
from llm_router.gateway.openai import OpenAIResponsesGateway
from llm_router.observability.lifecycle import ActiveObservationRegistry
from llm_router.observability.metrics import RouterMetrics
from llm_router.observability.models import TerminalStage


def _request(path: str, token: str = "wrong") -> Request:
    """Build one unauthenticated JSON request."""

    body = b"{}"

    async def receive() -> dict[str, object]:
        """Return one fixed body."""

        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [(b"x-api-key", token.encode()), (b"content-type", b"application/json")],
        },
        receive,
    )


def _runtime(events: list[Any]) -> SimpleNamespace:
    """Build the runtime surface needed before authentication."""

    observations = SimpleNamespace(record=events.append)
    metrics = RouterMetrics()
    return SimpleNamespace(
        config=RouterConfig.model_validate(router_config_data()),
        client_key="local-token",
        observations=observations,
        observation_registry=ActiveObservationRegistry(observations, metrics),
        metrics=metrics,
    )


def test_both_gateways_observe_auth_failure_and_return_trace_id() -> None:
    """Persist one early terminal fact for each protocol endpoint."""

    events: list[Any] = []
    runtime = _runtime(events)
    anthropic = asyncio.run(AnthropicGateway(runtime).handle(_request("/v1/messages")))
    openai = asyncio.run(OpenAIResponsesGateway(runtime).handle(_request("/v1/responses")))
    assert anthropic.status_code == openai.status_code == 401
    for response in (anthropic, openai):
        assert len(response.headers["x-llm-router-trace-id"]) == 32
        assert response.headers["x-llm-router-request-id"]
    assert [event.terminal_stage for event in events] == [
        TerminalStage.AUTHENTICATION,
        TerminalStage.AUTHENTICATION,
    ]
    assert all(event.protocol is None and event.profile is None for event in events)


def test_gateway_observation_excludes_request_content() -> None:
    """Keep client credentials and body content out of terminal facts."""

    events: list[Any] = []
    response = asyncio.run(
        AnthropicGateway(_runtime(events)).handle(
            _request("/v1/messages", "client-token-canary")
        )
    )
    serialized = repr(events[0])
    assert response.status_code == 401
    assert "client-token-canary" not in serialized
    assert "prompt-canary" not in serialized


def test_runtime_lifespan_starts_observation_hub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Start and close the fully assembled v0.7 runtime without an upstream call."""

    data = router_config_data()
    data["storage"] = {"sqlite_path": str(tmp_path / "router.db")}
    config_path = tmp_path / "router.yaml"
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setenv("LLM_ROUTER_CLIENT_API_KEY", "local-token")
    monkeypatch.setenv("ANTHROPIC_TEST_KEY", "anthropic-token")
    monkeypatch.setenv("OPENAI_TEST_KEY", "openai-token")
    with TestClient(create_app(str(config_path))) as client:
        assert client.get("/ready").status_code == 200
        assert client.get("/metrics").status_code == 200
        response = client.post("/v1/messages", json={})
        assert response.status_code == 401
        assert response.headers["x-llm-router-trace-id"]
