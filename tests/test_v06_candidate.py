"""Secret-free Candidate policy loading and catalog compatibility tests."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime

import yaml
from conftest import router_config_data

from llm_router.config import RouterConfig
from llm_router.routing.candidate import CandidatePolicyLoader


def _write_candidate(tmp_path, data: dict[str, object]) -> str:
    """Write one local candidate fixture and return its filename."""

    path = tmp_path / "candidate.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return str(path)


def test_candidate_load_does_not_resolve_provider_secrets(tmp_path, monkeypatch) -> None:
    """Compile a Candidate with all credential environment variables absent."""

    candidate_data = deepcopy(router_config_data())
    candidate_data["shadow"] = {"enabled": True, "candidate_config_path": "recursive.yaml"}
    candidate_path = _write_candidate(tmp_path, candidate_data)
    current_data = deepcopy(router_config_data())
    current_data["candidate_policy"] = {"config_path": candidate_path}
    monkeypatch.delenv("ANTHROPIC_TEST_KEY", raising=False)
    monkeypatch.delenv("OPENAI_TEST_KEY", raising=False)
    bundle = CandidatePolicyLoader(
        RouterConfig.model_validate(current_data), str(tmp_path / "router.yaml")
    ).load(datetime.now(UTC))
    assert bundle.catalog_compatible
    assert bundle.segments_compatible
    assert not bundle.config.shadow.enabled


def test_candidate_provider_or_target_rebinding_is_incompatible(tmp_path) -> None:
    """Reject executable identity changes even when routing policy can compile."""

    candidate_data = deepcopy(router_config_data())
    candidate_data["providers"]["anthropic"]["base_url"] = "https://changed.invalid"
    candidate_path = _write_candidate(tmp_path, candidate_data)
    current_data = deepcopy(router_config_data())
    current_data["candidate_policy"] = {"config_path": candidate_path}
    bundle = CandidatePolicyLoader(
        RouterConfig.model_validate(current_data), str(tmp_path / "router.yaml")
    ).load(datetime.now(UTC))
    assert not bundle.catalog_compatible
