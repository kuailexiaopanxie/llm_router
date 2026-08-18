"""Focused v0.4 evaluation, persistence, and replay acceptance tests."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
import yaml
from conftest import router_config_data
from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_router.app import create_app
from llm_router.cli import run_replay
from llm_router.config import RouterConfig
from llm_router.evaluation.codec import (
    encode_routing_request,
    make_policy_snapshot,
)
from llm_router.evaluation.models import (
    OutcomeEvent,
    OutcomeEvidence,
    OutcomeSource,
    OutcomeVerdict,
    ReplayChange,
    ReplayMode,
    ReplayStatus,
    RouteDecisionInput,
)
from llm_router.evaluation.outcomes import OutcomeService
from llm_router.evaluation.port import OutcomeConflictError
from llm_router.evaluation.replay import ReplayEngine
from llm_router.evaluation.sqlite_store import SQLiteEvaluationStore, SQLiteReplayStore
from llm_router.gateway.outcomes import register_outcome_route
from llm_router.health.coordinator import DisabledHealthCoordinator
from llm_router.routing.context import RoutingContext
from llm_router.routing.features import extract_routing_request
from llm_router.routing.kernel import RoutingKernel
from llm_router.routing.policy import compile_routing_policy


def test_policy_hash_excludes_connection_secrets_and_base_url() -> None:
    """Keep replay identity limited to routing semantics."""

    original = router_config_data()
    changed = deepcopy(original)
    changed["providers"]["anthropic"]["base_url"] = "https://different.invalid"
    changed["providers"]["anthropic"]["api_key_env"] = "DIFFERENT_SECRET_ENV"
    first = compile_routing_policy(RouterConfig.model_validate(original))
    second = compile_routing_policy(RouterConfig.model_validate(changed))
    assert first.routing_policy_hash == second.routing_policy_hash


def test_decision_codec_excludes_session_id_and_prompt() -> None:
    """Persist only bounded routing features, never request content or session keys."""

    request = extract_routing_request(
        {"messages": [{"role": "user", "content": "private prompt sentinel"}]},
        "code/auto",
    )
    payload = encode_routing_request(request)
    assert "private prompt sentinel" not in payload
    assert "private-session-sentinel" not in payload
    assert "session_id" not in payload


def test_outcome_store_is_atomic_idempotent_and_conflict_safe(tmp_path) -> None:
    """Accept one payload, deduplicate retries, and retain it on conflict."""

    async def exercise() -> None:
        store = SQLiteEvaluationStore(str(tmp_path / "router.db"))
        await store.start()
        now = datetime.now(UTC)
        event = OutcomeEvent(
            uuid4(),
            uuid4(),
            None,
            OutcomeVerdict.SUCCESS,
            OutcomeEvidence.TEST,
            OutcomeSource.CI,
            now,
            now,
        )
        first, duplicate = await asyncio.gather(
            store.submit_outcome(event),
            store.submit_outcome(event),
        )
        assert {first.status, duplicate.status} == {"accepted", "duplicate"}
        conflict = OutcomeEvent(
            event.event_id,
            event.request_id,
            None,
            OutcomeVerdict.FAILURE,
            event.evidence,
            event.source,
            event.observed_at,
            event.received_at,
        )
        with pytest.raises(OutcomeConflictError):
            await store.submit_outcome(conflict)
        await store.close()

    asyncio.run(exercise())


def test_historical_self_replay_is_unchanged_and_read_only(tmp_path) -> None:
    """Reproduce one captured plan using the same production Kernel."""

    async def prepare() -> tuple[RouterConfig, str]:
        config = RouterConfig.model_validate(router_config_data())
        path = str(tmp_path / "router.db")
        store = SQLiteEvaluationStore(path)
        await store.start()
        now = datetime.now(UTC)
        policy = compile_routing_policy(config)
        snapshot = make_policy_snapshot(config, now)
        await store.ensure_policy(snapshot)
        request = extract_routing_request(
            {"messages": [{"role": "user", "content": "hello"}]},
            "code/auto",
        )
        availability = DisabledHealthCoordinator(config.model_targets()).snapshot(now)
        plan = RoutingKernel(policy).plan(request, RoutingContext(None, availability))
        await store.append_decision(
            RouteDecisionInput(
                request_id=uuid4(),
                task_id=None,
                recorded_at=now,
                router_version="0.4.0",
                routing_algorithm_version=policy.routing_algorithm_version,
                routing_policy_hash=policy.routing_policy_hash,
                request=request,
                session=None,
                availability=availability,
                actual_plan=plan,
            )
        )
        await store.close()
        return config, path

    config, path = asyncio.run(prepare())
    before = (tmp_path / "router.db").read_bytes()
    case = next(SQLiteReplayStore(path).iter_cases(None, None, 10))
    result = ReplayEngine(compile_routing_policy(config), ReplayMode.HISTORICAL).replay(case)
    after = (tmp_path / "router.db").read_bytes()
    assert result.status is ReplayStatus.REPLAYED
    assert result.change is ReplayChange.UNCHANGED
    assert before == after


def test_outcome_http_statuses_are_strict_and_idempotent(tmp_path) -> None:
    """Expose accepted, duplicate, conflict, and strict validation statuses."""

    async def exercise() -> None:
        store = SQLiteEvaluationStore(str(tmp_path / "router.db"))
        await store.start()
        app = FastAPI()
        register_outcome_route(app, OutcomeService(store), "local-token", 16_384)
        event_id = str(uuid4())
        payload = {
            "event_id": event_id,
            "request_id": str(uuid4()),
            "verdict": "success",
            "evidence": "test",
            "source": "ci",
        }
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://router") as client:
            headers = {"x-api-key": "local-token"}
            accepted = await client.post("/v1/router/outcomes", json=payload, headers=headers)
            duplicate = await client.post("/v1/router/outcomes", json=payload, headers=headers)
            conflict = await client.post(
                "/v1/router/outcomes",
                json={**payload, "verdict": "failure"},
                headers=headers,
            )
            invalid = await client.post(
                "/v1/router/outcomes",
                json={**payload, "metadata": "forbidden"},
                headers=headers,
            )
        assert accepted.status_code == 201
        assert duplicate.status_code == 200
        assert conflict.status_code == 409
        assert invalid.status_code == 422
        await store.close()

    asyncio.run(exercise())


def test_application_lifecycle_migrates_evaluation_store(tmp_path, monkeypatch) -> None:
    """Start Runtime dependencies in order and expose the enabled endpoint."""

    data = router_config_data()
    data["storage"]["sqlite_path"] = str(tmp_path / "router.db")
    config_path = tmp_path / "router.yaml"
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setenv("LLM_ROUTER_CLIENT_API_KEY", "local-token")
    monkeypatch.setenv("ANTHROPIC_TEST_KEY", "anthropic-token")
    monkeypatch.setenv("OPENAI_TEST_KEY", "openai-token")
    app = create_app(str(config_path))
    with TestClient(app) as client:
        assert client.get("/ready").status_code == 200
        response = client.post(
            "/v1/router/outcomes",
            headers={"x-api-key": "local-token"},
            json={
                "event_id": str(uuid4()),
                "request_id": str(uuid4()),
                "verdict": "partial",
                "evidence": "task",
                "source": "client",
            },
        )
        assert response.status_code == 201
        assert response.json()["correlation"] == "pending"


def test_replay_cli_needs_no_provider_secrets_and_does_not_write(
    tmp_path, monkeypatch, capsys
) -> None:
    """Run an empty read-only report without resolving configured API keys."""

    data = router_config_data()
    database = tmp_path / "router.db"
    data["storage"]["sqlite_path"] = str(database)
    config_path = tmp_path / "candidate.yaml"
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")

    async def initialize() -> None:
        store = SQLiteEvaluationStore(str(database))
        await store.start()
        await store.close()

    asyncio.run(initialize())
    before = database.read_bytes()
    monkeypatch.delenv("ANTHROPIC_TEST_KEY", raising=False)
    monkeypatch.delenv("OPENAI_TEST_KEY", raising=False)
    result = run_replay(
        [
            "--db",
            str(database),
            "--candidate-config",
            str(config_path),
            "--format",
            "json",
        ]
    )
    output = capsys.readouterr()
    assert result == 0
    assert '"selected":0' in output.out
    assert output.err == ""
    assert database.read_bytes() == before
