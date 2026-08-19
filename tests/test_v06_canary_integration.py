"""Single-policy execution and fail-open Canary runtime integration tests."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import yaml
from conftest import router_config_data
from fastapi.testclient import TestClient

from llm_router.app import create_app
from llm_router.config import RouterConfig
from llm_router.errors import unknown_model
from llm_router.evaluation.recorder import NoopDecisionRecorder
from llm_router.health.coordinator import DisabledHealthCoordinator
from llm_router.routing.canary import PolicySelection
from llm_router.routing.coordinator import RoutingCoordinator, RoutingInvocation
from llm_router.routing.features import extract_routing_request
from llm_router.routing.policy import compile_routing_policy


def test_candidate_router_error_never_calls_current_kernel() -> None:
    """Keep the request on its selected Candidate after an expected route error."""

    config = RouterConfig.model_validate(router_config_data())
    policy = compile_routing_policy(config)
    calls = {"select": 0, "candidate": 0, "current": 0}

    def candidate_plan(*_: object) -> None:
        """Return one expected Candidate routing error."""

        calls["candidate"] += 1
        raise unknown_model()

    candidate = SimpleNamespace(policy=policy, plan=candidate_plan)

    def select(_: object) -> PolicySelection:
        """Select Candidate exactly once."""

        calls["select"] += 1
        return PolicySelection(candidate, None)

    health = DisabledHealthCoordinator(config.model_targets())
    coordinator = RoutingCoordinator(
        SimpleNamespace(select=select),
        SimpleNamespace(routing_snapshot=lambda _: None),
        health,
        NoopDecisionRecorder(),
    )
    invocation = RoutingInvocation(
        uuid4(),
        None,
        None,
        datetime.now(UTC),
        extract_routing_request({"messages": []}, "code/auto"),
    )
    resolution = coordinator.resolve(invocation)
    assert resolution.error is not None and resolution.error.code == "router_unknown_model"
    assert calls == {"select": 1, "candidate": 1, "current": 0}


def test_gate_not_met_keeps_application_ready_and_control_only(tmp_path, monkeypatch) -> None:
    """Start ready with an inactive fixed Selector when Shadow evidence is absent."""

    candidate_data = deepcopy(router_config_data())
    candidate_path = tmp_path / "candidate.yaml"
    candidate_path.write_text(yaml.safe_dump(candidate_data), encoding="utf-8")
    candidate_hash = compile_routing_policy(
        RouterConfig.model_validate(candidate_data)
    ).routing_policy_hash
    data = deepcopy(router_config_data())
    data["storage"]["sqlite_path"] = str(tmp_path / "router.db")
    data["replay"] = {"capture_enabled": True}
    data["candidate_policy"] = {
        "config_path": str(candidate_path),
        "expected_policy_hash": candidate_hash,
    }
    data["canary"] = {
        "enabled": True,
        "traffic_rate": "0.01",
        "segments": [{"protocol": "anthropic_messages", "profile": "code/auto"}],
        "minimum_shadow_evaluated": 1,
    }
    config_path = tmp_path / "router.yaml"
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setenv("LLM_ROUTER_CLIENT_API_KEY", "local-token")
    monkeypatch.setenv("ANTHROPIC_TEST_KEY", "anthropic-token")
    monkeypatch.setenv("OPENAI_TEST_KEY", "openai-token")
    monkeypatch.setenv("LLM_ROUTER_CANARY_SALT", "s" * 32)
    with TestClient(create_app(str(config_path))) as client:
        assert client.get("/ready").status_code == 200
        runtime = client.app.state.runtime
        assert runtime.coordinator is not None
        assert not runtime.canary_state.active
        assert runtime.canary_state.reason.value == "shadow_gate_not_met"
