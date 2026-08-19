"""Secret-free Candidate policy loading and executable catalog validation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from llm_router.config import RouterConfig, load_candidate_config
from llm_router.evaluation.codec import make_policy_snapshot
from llm_router.evaluation.models import RoutingPolicySnapshot
from llm_router.routing.kernel import RoutingKernel
from llm_router.routing.policy import RoutingPolicy, compile_routing_policy


@dataclass(frozen=True, slots=True)
class CandidateBundle:
    """Contain one immutable compiled candidate and compatibility facts."""

    config: RouterConfig
    policy: RoutingPolicy
    kernel: RoutingKernel
    snapshot: RoutingPolicySnapshot
    expected_policy_hash: str | None
    catalog_compatible: bool
    segments_compatible: bool


def _provider_identity(config: RouterConfig, name: str) -> tuple[object, ...] | None:
    """Return all Provider fields that affect actual executable identity."""

    provider = config.providers.get(name)
    if provider is None:
        return None
    return (
        provider.type,
        provider.base_url,
        provider.auth_scheme,
        provider.api_key_env,
        provider.extension_headers,
        provider.max_concurrency,
    )


def _target_identity(config: RouterConfig, alias: str) -> tuple[object, ...] | None:
    """Return all target fields that Candidate is forbidden to modify."""

    model = config.models.get(alias)
    if model is None:
        return None
    return (
        model.provider,
        model.upstream_model,
        model.protocol,
        model.tier,
        model.capabilities,
        model.max_input_tokens,
        model.input_price_per_million,
        model.output_price_per_million,
        model.state_scope,
    )


def catalog_compatible(current: RouterConfig, candidate: RouterConfig) -> bool:
    """Require every Candidate Provider and target to match the Current catalog."""

    for alias, model in candidate.models.items():
        if _target_identity(current, alias) != _target_identity(candidate, alias):
            return False
        if _provider_identity(current, model.provider) != _provider_identity(candidate, model.provider):
            return False
    return True


def segments_compatible(current: RouterConfig, candidate: RouterConfig) -> bool:
    """Require every Canary segment to exist for both compiled policies."""

    current_policy = compile_routing_policy(current)
    candidate_policy = compile_routing_policy(candidate)
    for segment in current.canary.segments:
        current_profile = current_policy.profiles.get(segment.profile)
        candidate_profile = candidate_policy.profiles.get(segment.profile)
        if current_profile is None or candidate_profile is None:
            return False
        if current_profile.automatic and not any(
            target.protocol is segment.protocol for target in current_policy.targets.values()
        ):
            return False
        if candidate_profile.automatic and not any(
            target.protocol is segment.protocol for target in candidate_policy.targets.values()
        ):
            return False
        if not current_profile.automatic and segment.protocol not in current_profile.targets:
            return False
        if not candidate_profile.automatic and segment.protocol not in candidate_profile.targets:
            return False
    return True


class CandidatePolicyLoader:
    """Load one local Candidate without resolving credentials or creating Providers."""

    def __init__(self, current: RouterConfig, main_config_path: str) -> None:
        """Bind Current config and its directory for relative path resolution."""

        self._current = current
        self._main_path = Path(main_config_path).expanduser()

    def load(self, created_at: datetime) -> CandidateBundle:
        """Compile and validate one fixed Candidate policy bundle."""

        source = (
            self._current.candidate_policy.config_path
            if self._current.candidate_policy is not None
            else self._current.shadow.candidate_config_path
        )
        if source is None:
            raise ValueError("candidate policy source is unavailable")
        path = Path(source).expanduser()
        if not path.is_absolute():
            path = self._main_path.parent / path
        candidate = load_candidate_config(path)
        policy = compile_routing_policy(candidate)
        current_policy = compile_routing_policy(self._current)
        if (
            policy.schema_version != current_policy.schema_version
            or policy.routing_algorithm_version != current_policy.routing_algorithm_version
        ):
            raise ValueError("candidate routing policy is incompatible")
        expected = (
            self._current.candidate_policy.expected_policy_hash
            if self._current.candidate_policy is not None
            else None
        )
        return CandidateBundle(
            candidate,
            policy,
            RoutingKernel(policy),
            make_policy_snapshot(candidate, created_at),
            expected,
            catalog_compatible(self._current, candidate),
            segments_compatible(self._current, candidate),
        )
