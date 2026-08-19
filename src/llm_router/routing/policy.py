"""Pure routing policy compilation and stable identity."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from llm_router.config import AutoProfileConfig, ExplicitProfileConfig
from llm_router.domain import ExecutionTimeouts, ModelTarget, Protocol

if TYPE_CHECKING:
    from llm_router.config import RouterConfig

ROUTING_POLICY_SCHEMA_VERSION = 1
ROUTING_ALGORITHM_VERSION = "v1"


@dataclass(frozen=True, slots=True)
class TargetChain:
    """Store one protocol-specific ordered target chain."""

    primary: str
    fallback: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProfilePolicy:
    """Store either automatic mode or explicit protocol chains."""

    automatic: bool
    targets: Mapping[Protocol, TargetChain]


@dataclass(frozen=True, slots=True)
class RoutingPolicy:
    """Contain only immutable fields consumed by the routing kernel."""

    targets: Mapping[str, ModelTarget]
    profiles: Mapping[str, ProfilePolicy]
    default_profile: str
    policy_version: str
    failure_escalation_requests: int
    fast_max_input_tokens: int
    balanced_max_input_tokens: int
    deep_tool_rounds_threshold: int
    attempt_limit: int
    timeouts: ExecutionTimeouts
    routing_policy_hash: str
    schema_version: int = ROUTING_POLICY_SCHEMA_VERSION
    routing_algorithm_version: str = ROUTING_ALGORITHM_VERSION

    @property
    def effective_policy_version(self) -> str:
        """Return legacy policy metadata used by online observations."""

        return f"{self.policy_version}-{self.routing_policy_hash[:12]}"


def policy_document(config: RouterConfig) -> dict[str, object]:
    """Build the secret-free canonical policy document."""

    targets = {
        alias: {
            "provider": model.provider,
            "upstream_model": model.upstream_model,
            "tier": model.tier.value,
            "capabilities": sorted(item.value for item in model.capabilities),
            "max_input_tokens": model.max_input_tokens,
            "input_price_per_million": model.input_price_per_million,
            "output_price_per_million": model.output_price_per_million,
            "protocol": model.protocol.value,
            "state_scope": model.state_scope,
        }
        for alias, model in sorted(config.models.items())
    }
    profiles: dict[str, object] = {}
    for name, profile in sorted(config.profiles.items()):
        if isinstance(profile, AutoProfileConfig):
            profiles[name] = {"mode": "auto", "targets": {}}
        else:
            assert isinstance(profile, ExplicitProfileConfig)
            profiles[name] = {
                "mode": "explicit",
                "targets": {
                    protocol.value: {
                        "primary": chain.primary,
                        "fallback": list(chain.fallback),
                    }
                    for protocol, chain in sorted(profile.targets.items(), key=lambda item: item[0].value)
                },
            }
    routing = config.routing
    return {
        "schema_version": ROUTING_POLICY_SCHEMA_VERSION,
        "routing_algorithm_version": ROUTING_ALGORITHM_VERSION,
        "policy_version": routing.policy_version,
        "targets": targets,
        "profiles": profiles,
        "default_profile": routing.default_profile,
        "failure_escalation_requests": routing.failure_escalation_requests,
        "fast_max_input_tokens": routing.fast_max_input_tokens,
        "balanced_max_input_tokens": routing.balanced_max_input_tokens,
        "deep_tool_rounds_threshold": routing.deep_tool_rounds_threshold,
        "attempt_limit": routing.attempt_limit,
        "timeouts": config.timeouts.model_dump(mode="json"),
    }


def canonical_policy_json(config: RouterConfig) -> str:
    """Serialize a policy document with deterministic JSON rules."""

    return json.dumps(policy_document(config), sort_keys=True, separators=(",", ":"))


def compile_routing_policy(config: RouterConfig) -> RoutingPolicy:
    """Compile validated application config into a pure routing policy."""

    document = canonical_policy_json(config)
    digest = hashlib.sha256(document.encode()).hexdigest()
    profiles: dict[str, ProfilePolicy] = {}
    for name, profile in config.profiles.items():
        if isinstance(profile, AutoProfileConfig):
            profiles[name] = ProfilePolicy(True, MappingProxyType({}))
            continue
        assert isinstance(profile, ExplicitProfileConfig)
        profiles[name] = ProfilePolicy(
            False,
            MappingProxyType(
                {
                    protocol: TargetChain(chain.primary, chain.fallback)
                    for protocol, chain in profile.targets.items()
                }
            ),
        )
    routing = config.routing
    return RoutingPolicy(
        targets=MappingProxyType(config.model_targets()),
        profiles=MappingProxyType(profiles),
        default_profile=routing.default_profile,
        policy_version=routing.policy_version,
        failure_escalation_requests=routing.failure_escalation_requests,
        fast_max_input_tokens=routing.fast_max_input_tokens,
        balanced_max_input_tokens=routing.balanced_max_input_tokens,
        deep_tool_rounds_threshold=routing.deep_tool_rounds_threshold,
        attempt_limit=routing.attempt_limit,
        timeouts=config.timeouts.to_domain(),
        routing_policy_hash=digest,
    )
