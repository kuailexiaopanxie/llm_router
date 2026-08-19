"""Acceptance tests for v0.5 online shadow policy evaluation."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import yaml
from conftest import router_config_data
from fastapi.testclient import TestClient

from llm_router.app import create_app
from llm_router.cli import run_shadow_report
from llm_router.config import RouterConfig, load_candidate_config
from llm_router.evaluation.codec import make_policy_snapshot
from llm_router.evaluation.models import (
    ReplayChange,
    RouteDecisionInput,
    ShadowDecision,
    ShadowStatus,
)
from llm_router.evaluation.port import ShadowIntegrityError
from llm_router.evaluation.replay import ReplayEngine
from llm_router.evaluation.shadow import ShadowEvaluator
from llm_router.evaluation.shadow_sqlite import SQLiteShadowReader
from llm_router.evaluation.sqlite_store import SQLiteEvaluationStore
from llm_router.health.coordinator import DisabledHealthCoordinator
from llm_router.observability.metrics import RouterMetrics
from llm_router.routing.context import RoutingContext
from llm_router.routing.features import extract_routing_request
from llm_router.routing.kernel import RoutingKernel
from llm_router.routing.policy import compile_routing_policy


def test_shadow_config_defaults_and_candidate_loader_ignores_recursion(tmp_path) -> None:
    """Keep old config disabled and compile a candidate without its shadow block."""

    data = router_config_data()
    config = RouterConfig.model_validate(data)
    assert not config.shadow.enabled
    candidate = deepcopy(data)
    candidate["shadow"] = {
        "enabled": True,
        "candidate_config_path": "recursive.yaml",
        "sample_rate": 1.0,
    }
    candidate_path = tmp_path / "candidate.yaml"
    candidate_path.write_text(yaml.safe_dump(candidate), encoding="utf-8")
    loaded = load_candidate_config(candidate_path)
    assert not loaded.shadow.enabled
    assert compile_routing_policy(loaded).routing_policy_hash


def test_shadow_config_rejects_unsafe_path_and_unknown_profile() -> None:
    """Require a local path and a declared profile filter."""

    data = router_config_data()
    data["shadow"] = {
        "enabled": True,
        "candidate_config_path": "https://candidate.invalid/router.yaml",
    }
    with pytest.raises(ValueError, match="local file path"):
        RouterConfig.model_validate(data)
    data["shadow"]["candidate_config_path"] = "candidate.yaml"
    data["shadow"]["profiles"] = ["code/missing"]
    with pytest.raises(ValueError, match="declared profiles"):
        RouterConfig.model_validate(data)


def test_shadow_evaluator_is_deterministic_and_self_comparison_is_unchanged(tmp_path) -> None:
    """Persist one self-replayed comparison without touching a Provider."""

    async def exercise() -> None:
        config = RouterConfig.model_validate(router_config_data())
        now = datetime.now(UTC)
        policy = compile_routing_policy(config)
        snapshot = make_policy_snapshot(config, now)
        store = SQLiteEvaluationStore(str(tmp_path / "router.db"))
        await store.start()
        await store.ensure_policy(snapshot)
        request = extract_routing_request(
            {"messages": [{"role": "user", "content": "shadow prompt sentinel"}]}, "code/auto"
        )
        availability = DisabledHealthCoordinator(config.model_targets()).snapshot(now)
        plan = RoutingKernel(policy).plan(request, RoutingContext(None, availability))
        decision = RouteDecisionInput(
            request_id=uuid4(),
            task_id=None,
            recorded_at=now,
            router_version="0.5.0",
            routing_algorithm_version=policy.routing_algorithm_version,
            routing_policy_hash=policy.routing_policy_hash,
            request=request,
            session=None,
            availability=availability,
            actual_plan=plan,
        )
        metrics = RouterMetrics()
        evaluator = ShadowEvaluator(
            ReplayEngine(policy, "historical"),
            snapshot,
            store,
            1.0,
            frozenset(),
            frozenset(),
            1,
            1_000,
            metrics=metrics,
        )
        assert evaluator._sampled(decision.request_id)
        await evaluator.start()
        evaluator.submit(decision)
        await evaluator.close()
        rows = list(SQLiteShadowReader(str(tmp_path / "router.db")).iter_shadow(None, None, None, 10))
        assert len(rows) == 1
        assert rows[0].status is ShadowStatus.EVALUATED
        assert rows[0].change is not None and rows[0].change.value == "unchanged"
        assert "shadow prompt sentinel" not in (tmp_path / "router.db").read_text(
            encoding="utf-8", errors="ignore"
        )
        await store.close()

    asyncio.run(exercise())


def test_shadow_store_is_idempotent_and_conflict_safe(tmp_path) -> None:
    """Retain the first immutable comparison for a request and candidate pair."""

    async def exercise() -> None:
        config = RouterConfig.model_validate(router_config_data())
        now = datetime.now(UTC)
        policy = compile_routing_policy(config)
        snapshot = make_policy_snapshot(config, now)
        store = SQLiteEvaluationStore(str(tmp_path / "router.db"))
        await store.start()
        await store.ensure_policy(snapshot)
        request = extract_routing_request({"messages": []}, "code/auto")
        availability = DisabledHealthCoordinator(config.model_targets()).snapshot(now)
        plan = RoutingKernel(policy).plan(request, RoutingContext(None, availability))
        decision = ShadowDecision(
            request_id=uuid4(),
            recorded_at=now,
            evaluated_at=now,
            protocol=request.protocol,
            requested_profile="code/auto",
            actual_policy_hash=policy.routing_policy_hash,
            candidate_policy_hash=policy.routing_policy_hash,
            candidate_algorithm_version=policy.routing_algorithm_version,
            actual_plan=plan,
            actual_error=None,
            candidate_plan=plan,
            candidate_error=None,
            status=ShadowStatus.EVALUATED,
            change=ReplayChange.UNCHANGED,
        )
        assert await store.append_shadow(decision) == "written"
        assert await store.append_shadow(decision) == "duplicate"
        changed = replace(decision, evaluated_at=datetime.now(UTC))
        with pytest.raises(ShadowIntegrityError):
            await store.append_shadow(changed)
        await store.close()

    asyncio.run(exercise())


def test_shadow_report_is_read_only_and_has_no_quality_claims(tmp_path, capsys) -> None:
    """Render bounded persisted facts without modifying SQLite."""

    async def prepare() -> None:
        config = RouterConfig.model_validate(router_config_data())
        now = datetime.now(UTC)
        policy = compile_routing_policy(config)
        snapshot = make_policy_snapshot(config, now)
        store = SQLiteEvaluationStore(str(tmp_path / "router.db"))
        await store.start()
        await store.ensure_policy(snapshot)
        request = extract_routing_request({"messages": []}, "code/auto")
        availability = DisabledHealthCoordinator(config.model_targets()).snapshot(now)
        plan = RoutingKernel(policy).plan(request, RoutingContext(None, availability))
        await store.append_shadow(
            ShadowDecision(
                uuid4(), now, now, request.protocol, "code/auto", policy.routing_policy_hash,
                policy.routing_policy_hash, policy.routing_algorithm_version, plan, None, plan, None,
                ShadowStatus.EVALUATED, ReplayChange.UNCHANGED,
            )
        )
        await store.close()

    asyncio.run(prepare())
    before = (tmp_path / "router.db").read_bytes()
    assert run_shadow_report(["--db", str(tmp_path / "router.db"), "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    after = (tmp_path / "router.db").read_bytes()
    assert before == after
    assert payload["changes"] == {"unchanged": 1}
    assert all(term not in json.dumps(payload) for term in ("quality", "latency", "cost", "success_rate"))


def test_invalid_shadow_candidate_keeps_application_ready(tmp_path, monkeypatch) -> None:
    """Keep actual Runtime startup independent from candidate load failure."""

    data = router_config_data()
    data["storage"]["sqlite_path"] = str(tmp_path / "router.db")
    data["shadow"] = {"enabled": True, "candidate_config_path": "missing.yaml", "sample_rate": 1.0}
    config_path = tmp_path / "router.yaml"
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setenv("LLM_ROUTER_CLIENT_API_KEY", "local-token")
    monkeypatch.setenv("ANTHROPIC_TEST_KEY", "anthropic-token")
    monkeypatch.setenv("OPENAI_TEST_KEY", "openai-token")
    with TestClient(create_app(str(config_path))) as client:
        assert client.get("/ready").status_code == 200
