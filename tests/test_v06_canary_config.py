"""Controlled Canary configuration and affinity header acceptance tests."""

from __future__ import annotations

from copy import deepcopy

import pytest
from conftest import router_config_data

from llm_router.config import RouterConfig
from llm_router.errors import RouterError
from llm_router.gateway.auth import safe_headers, session_id


def _enabled_data() -> dict[str, object]:
    """Return a minimally enabled Canary configuration."""

    data = deepcopy(router_config_data())
    data["candidate_policy"] = {
        "config_path": "candidate.yaml",
        "expected_policy_hash": "a" * 64,
    }
    data["canary"] = {
        "enabled": True,
        "traffic_rate": "0.0100",
        "segments": [{"protocol": "anthropic_messages", "profile": "code/auto"}],
    }
    return data


def test_canary_config_accepts_exact_rate_and_legacy_path_alias() -> None:
    """Normalize a matching Shadow source while retaining exact threshold math."""

    data = _enabled_data()
    data["shadow"] = {"candidate_config_path": "./candidate.yaml"}
    config = RouterConfig.model_validate(data)
    assert config.canary.threshold == 100
    assert config.candidate_policy is not None
    assert config.candidate_policy.expected_policy_hash == "a" * 64


@pytest.mark.parametrize("rate", ["0.00001", "0.2501", "0.0000"])
def test_enabled_canary_rejects_unsafe_rates(rate: str) -> None:
    """Reject excess precision and rates outside the enabled safety boundary."""

    data = _enabled_data()
    data["canary"]["traffic_rate"] = rate  # type: ignore[index]
    with pytest.raises(ValueError):
        RouterConfig.model_validate(data)


def test_canary_rejects_duplicate_or_unknown_segments() -> None:
    """Require unique segments backed by declared profiles."""

    data = _enabled_data()
    segment = {"protocol": "anthropic_messages", "profile": "code/auto"}
    data["canary"]["segments"] = [segment, segment]  # type: ignore[index]
    with pytest.raises(ValueError, match="unique"):
        RouterConfig.model_validate(data)
    data["canary"]["segments"] = [  # type: ignore[index]
        {"protocol": "anthropic_messages", "profile": "code/missing"}
    ]
    with pytest.raises(ValueError, match="declared profile"):
        RouterConfig.model_validate(data)


def test_session_affinity_is_validated_and_never_forwarded() -> None:
    """Keep bounded opaque session affinity local to the Router."""

    headers = {"x-llm-router-session-id": "会话-1", "content-type": "application/json"}
    assert session_id(headers) == "会话-1"
    assert "x-llm-router-session-id" not in safe_headers(headers)
    with pytest.raises(RouterError, match="without control characters"):
        session_id({"x-llm-router-session-id": "bad\x7fvalue"})
    with pytest.raises(RouterError, match="1 to 256"):
        session_id({"x-llm-router-session-id": "x" * 257})
