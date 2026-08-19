"""v0.7 persisted and rendered observation privacy tests."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from conftest import router_config_data
from fastapi import Request

from llm_router.config import RouterConfig
from llm_router.gateway.anthropic import AnthropicGateway
from llm_router.observability.cli import run_routes
from llm_router.observability.lifecycle import ActiveObservationRegistry
from llm_router.observability.metrics import RouterMetrics
from llm_router.observability.models import CostEstimate, CostStatus, ObservationBundle
from llm_router.observability.sqlite_store import SQLiteObservationStore
from llm_router.observability.tracing import TraceBuilder

_SENTINELS = (
    "prompt-canary",
    "response-canary",
    "session-canary",
    "tool-argument-canary",
    "provider-secret-canary",
    "client-token-canary",
    "otlp-header-canary",
)


def _private_request() -> Request:
    """Build one unauthorized request containing every privacy sentinel."""

    body = json.dumps(
        {
            "model": "code/auto",
            "messages": [{"role": "user", "content": "prompt-canary"}],
            "response": "response-canary",
            "tool": {"arguments": "tool-argument-canary"},
            "provider_secret": "provider-secret-canary",
            "otlp_header": "otlp-header-canary",
        }
    ).encode()
    sent = False

    async def receive() -> dict[str, object]:
        """Return the private body once."""

        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/messages",
            "headers": [
                (b"x-api-key", b"client-token-canary"),
                (b"x-llm-router-session-id", b"session-canary"),
                (b"content-type", b"application/json"),
            ],
        },
        receive,
    )


def test_observation_sinks_exclude_private_request_values(
    tmp_path: Path, capsys: Any
) -> None:
    """Keep all fixed privacy sentinels out of DB, Metrics, and CLI output."""

    events: list[Any] = []
    metrics = RouterMetrics()
    sink = SimpleNamespace(record=events.append)
    runtime = SimpleNamespace(
        config=RouterConfig.model_validate(router_config_data()),
        client_key="local-token",
        observations=sink,
        observation_registry=ActiveObservationRegistry(sink, metrics),
        metrics=metrics,
    )
    response = asyncio.run(AnthropicGateway(runtime).handle(_private_request()))
    assert response.status_code == 401
    event = events[0]
    cost = CostEstimate(CostStatus.NOT_APPLICABLE, None, None, None)
    bundle = ObservationBundle(
        event,
        cost,
        TraceBuilder(Decimal(1)).build(event, cost),
    )
    metrics.record_observation(bundle)
    path = tmp_path / "router.db"

    async def persist() -> None:
        """Persist the sanitized terminal bundle."""

        store = SQLiteObservationStore(str(path))
        await store.start()
        await store.append(bundle)
        await store.close()

    asyncio.run(persist())
    assert run_routes(["--db", str(path), "--format", "json"]) == 0
    rendered = "\n".join(
        (repr(event), metrics.render().decode(), capsys.readouterr().out)
    ).encode()
    database = path.read_bytes()
    for sentinel in _SENTINELS:
        encoded = sentinel.encode()
        assert encoded not in rendered
        assert encoded not in database
