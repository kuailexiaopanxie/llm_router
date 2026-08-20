"""Strict YAML configuration loading and startup validation."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from llm_router.canary_config import CanaryConfig, CandidatePolicyConfig
from llm_router.dashboard.config import DashboardConfig
from llm_router.domain import Capability, ExecutionTimeouts, ModelTarget, Protocol, Tier
from llm_router.observability.config import ObservabilityConfig, PricingConfig

MAX_REQUEST_BYTES = 64 * 1024 * 1024


class StrictModel(BaseModel):
    """Reject unknown configuration keys in every nested object."""

    model_config = ConfigDict(extra="forbid")


class ServerConfig(StrictModel):
    """Local HTTP listener configuration."""

    host: str = "127.0.0.1"
    port: int = Field(default=8848, ge=1, le=65535)
    client_api_key_env: str = "LLM_ROUTER_CLIENT_API_KEY"
    max_request_bytes: int = Field(default=16 * 1024 * 1024, gt=0, le=MAX_REQUEST_BYTES)
    allow_remote_access: bool = False

    @model_validator(mode="after")
    def validate_listener(self) -> ServerConfig:
        """Require explicit opt-in before listening on a non-loopback address."""

        try:
            is_loopback = ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            is_loopback = self.host == "localhost"
        if not is_loopback and not self.allow_remote_access:
            raise ValueError("non-loopback host requires allow_remote_access=true")
        return self


class StorageConfig(StrictModel):
    """SQLite persistence location."""

    sqlite_path: str = "~/.llm-router/router.db"
    queue_capacity: int = Field(default=2048, ge=1, le=100_000)


class OutcomesConfig(StrictModel):
    """Bounded synchronous Outcome Event intake settings."""

    enabled: bool = True
    max_request_bytes: int = Field(default=16_384, ge=1_024, le=65_536)
    max_event_age_seconds: int = Field(default=604_800, ge=60, le=2_592_000)
    max_future_skew_seconds: int = Field(default=300, ge=0, le=3_600)


class ReplayConfig(StrictModel):
    """Bounded offline replay settings."""

    capture_enabled: bool = True
    max_records: int = Field(default=10_000, ge=1, le=100_000)


class ShadowConfig(StrictModel):
    """Bound online shadow policy evaluation settings."""

    enabled: bool = False
    candidate_config_path: str | None = None
    sample_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    protocols: tuple[Protocol, ...] = ()
    profiles: tuple[str, ...] = ()
    queue_capacity: int = Field(default=256, ge=1, le=10_000)
    evaluation_timeout_ms: int = Field(default=25, ge=1, le=1_000)

    @model_validator(mode="after")
    def validate_shadow(self) -> ShadowConfig:
        """Reject unsafe paths, invalid profiles, and over-precise rates."""

        exponent = Decimal(str(self.sample_rate)).as_tuple().exponent
        if isinstance(exponent, int) and exponent < -4:
            raise ValueError("shadow.sample_rate supports at most four decimal places")
        path = self.candidate_config_path
        if self.enabled and (path is None or not path.strip()):
            raise ValueError("shadow.candidate_config_path is required when shadow is enabled")
        if path is not None:
            if path != path.strip() or any(
                token in path for token in ("://", "$", "`", ";", "&&", "||", "|", "\n", "\r", "\0")
            ):
                raise ValueError("shadow.candidate_config_path must be a local file path")
            if path.startswith((">", "<")):
                raise ValueError("shadow.candidate_config_path must be a local file path")
        alias_pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
        if any(not alias_pattern.fullmatch(profile) for profile in self.profiles):
            raise ValueError("shadow.profiles contains an invalid profile name")
        return self


class HealthConfig(StrictModel):
    """Bounded in-memory Provider and Model Target health policy."""

    enabled: bool = True
    failure_threshold: int = Field(default=2, ge=1, le=10)
    failure_window_seconds: int = Field(default=120, ge=10, le=86_400)
    cooldown_seconds: float = Field(default=30, ge=1, le=3_600)
    max_cooldown_seconds: float = Field(default=300, ge=1, le=86_400)
    backoff_multiplier: float = Field(default=2.0, ge=1.0, le=8.0)

    @model_validator(mode="after")
    def validate_cooldown(self) -> HealthConfig:
        """Keep the maximum cooldown above the initial cooldown."""

        if self.max_cooldown_seconds < self.cooldown_seconds:
            raise ValueError("max_cooldown_seconds must be at least cooldown_seconds")
        return self


class ProviderConfig(StrictModel):
    """Protocol provider connection settings."""

    type: Literal["anthropic", "openai"]
    base_url: str
    api_key_env: str
    auth_scheme: Literal["x_api_key", "bearer"] = "x_api_key"
    max_concurrency: int = Field(default=16, ge=1, le=1024)
    extension_headers: tuple[str, ...] = ()
    propagate_trace_context: bool = False

    @model_validator(mode="after")
    def validate_base_url(self) -> ProviderConfig:
        """Allow only fixed HTTP(S) upstream origins from configuration."""

        if not self.base_url.startswith(("https://", "http://")):
            raise ValueError("base_url must use http or https")
        if self.type == "openai" and self.auth_scheme != "bearer":
            raise ValueError("openai providers require auth_scheme='bearer'")
        return self


class ModelConfig(StrictModel):
    """Configured model alias and hard capability declaration."""

    provider: str
    upstream_model: str
    tier: Tier
    capabilities: frozenset[Capability]
    max_input_tokens: int = Field(gt=0)
    input_price_per_million: float | None = Field(default=None, ge=0)
    output_price_per_million: float | None = Field(default=None, ge=0)
    pricing: PricingConfig | None = None
    protocol: Protocol = Protocol.ANTHROPIC_MESSAGES
    state_scope: str | None = Field(
        default=None,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$",
    )

    @model_validator(mode="after")
    def validate_model_identity(self) -> ModelConfig:
        """Reject empty upstream identifiers that would produce ambiguous requests."""

        if not self.provider.strip() or not self.upstream_model.strip():
            raise ValueError("model provider and upstream_model must be non-empty")
        if self.pricing is not None and (
            self.input_price_per_million is not None
            or self.output_price_per_million is not None
        ):
            raise ValueError("legacy and versioned model pricing cannot be configured together")
        return self


class AutoProfileConfig(StrictModel):
    """Marker for the deterministic automatic policy."""

    mode: Literal["auto"]


class TargetChainConfig(StrictModel):
    """Ordered primary and fallback aliases for one inbound protocol."""

    primary: str
    fallback: tuple[str, ...] = ()


class ExplicitProfileConfig(StrictModel):
    """Protocol-specific ordered model aliases for an explicit route profile."""

    targets: dict[Protocol, TargetChainConfig] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def migrate_v01_chain(cls, value: object) -> object:
        """Migrate the v0.1 primary/fallback shape into an Anthropic chain."""

        if not isinstance(value, dict) or "targets" in value:
            return value
        primary = value.get("primary")
        if primary is None:
            return value
        return {
            "targets": {
                Protocol.ANTHROPIC_MESSAGES.value: {
                    "primary": primary,
                    "fallback": value.get("fallback", ()),
                }
            }
        }

    def chain_for(self, protocol: Protocol) -> TargetChainConfig | None:
        """Return the configured target chain for one inbound protocol."""

        return self.targets.get(protocol)


ProfileConfig = Annotated[AutoProfileConfig | ExplicitProfileConfig, Field(union_mode="left_to_right")]


class RoutingConfig(StrictModel):
    """Versioned deterministic policy thresholds."""

    default_profile: str = "code/auto"
    policy_version: str = "v2"
    session_ttl_seconds: int = Field(default=7200, ge=1)
    session_capacity: int = Field(default=10_000, ge=1)
    failure_escalation_requests: int = Field(default=2, ge=1)
    fast_max_input_tokens: int = Field(default=8000, ge=1)
    balanced_max_input_tokens: int = Field(default=64_000, ge=1)
    deep_tool_rounds_threshold: int = Field(default=3, ge=1)
    attempt_limit: int = Field(default=2, ge=1, le=10)

    @model_validator(mode="after")
    def validate_thresholds(self) -> RoutingConfig:
        """Keep token tier thresholds monotonically increasing."""

        if self.fast_max_input_tokens >= self.balanced_max_input_tokens:
            raise ValueError("fast_max_input_tokens must be below balanced_max_input_tokens")
        return self


class TimeoutConfig(StrictModel):
    """Network and total request timeout budgets."""

    connect_seconds: float = Field(default=5, gt=0)
    response_header_seconds: float = Field(default=30, gt=0)
    non_stream_deadline_seconds: float = Field(default=120, gt=0)
    stream_idle_seconds: float = Field(default=90, gt=0)
    stream_max_seconds: float = Field(default=1800, gt=0)

    def to_domain(self) -> ExecutionTimeouts:
        """Convert configuration into an immutable plan value."""

        return ExecutionTimeouts(**self.model_dump())


class RouterConfig(StrictModel):
    """Complete validated router configuration."""

    version: Literal[1]
    server: ServerConfig
    storage: StorageConfig
    outcomes: OutcomesConfig = Field(default_factory=OutcomesConfig)
    replay: ReplayConfig = Field(default_factory=ReplayConfig)
    shadow: ShadowConfig = Field(default_factory=ShadowConfig)
    candidate_policy: CandidatePolicyConfig | None = None
    canary: CanaryConfig = Field(default_factory=CanaryConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    health: HealthConfig = Field(default_factory=HealthConfig)
    providers: dict[str, ProviderConfig]
    models: dict[str, ModelConfig]
    profiles: dict[str, ProfileConfig]
    routing: RoutingConfig
    timeouts: TimeoutConfig

    @model_validator(mode="before")
    @classmethod
    def migrate_observation_queue_alias(cls, value: object) -> object:
        """Use the legacy storage queue as the observability queue default."""

        if not isinstance(value, dict):
            return value
        result = dict(value)
        storage = result.get("storage")
        observability = result.get("observability")
        if isinstance(storage, dict) and "queue_capacity" in storage:
            observed = dict(observability) if isinstance(observability, dict) else {}
            observed.setdefault("queue_capacity", storage["queue_capacity"])
            result["observability"] = observed
        return result

    @model_validator(mode="after")
    def validate_references(self) -> RouterConfig:
        """Validate provider, profile, capability, and fallback graph integrity."""

        alias_pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
        if self.storage.queue_capacity != self.observability.queue_capacity:
            raise ValueError("storage and observability queue_capacity must match")
        try:
            listener_loopback = ipaddress.ip_address(self.server.host).is_loopback
        except ValueError:
            listener_loopback = self.server.host == "localhost"
        if not listener_loopback and not self.observability.metrics.require_auth:
            raise ValueError("remote metrics exposure requires metrics.require_auth=true")
        if not listener_loopback and self.dashboard.enabled and not self.dashboard.require_auth:
            raise ValueError("remote dashboard exposure requires dashboard.require_auth=true")
        invalid_aliases = [alias for alias in self.models if not alias_pattern.fullmatch(alias)]
        if invalid_aliases:
            raise ValueError(f"invalid model alias: {invalid_aliases[0]!r}")
        invalid_profiles = [name for name in self.profiles if not alias_pattern.fullmatch(name)]
        if invalid_profiles:
            raise ValueError(f"invalid profile name: {invalid_profiles[0]!r}")
        if self.routing.default_profile not in self.profiles:
            raise ValueError("routing.default_profile must reference a declared profile")
        unknown_shadow_profiles = [
            profile for profile in self.shadow.profiles if profile not in self.profiles
        ]
        if unknown_shadow_profiles:
            raise ValueError("shadow.profiles must reference declared profiles")
        candidate_path = self.candidate_policy.config_path if self.candidate_policy else None
        shadow_path = self.shadow.candidate_config_path
        if (
            candidate_path
            and shadow_path
            and Path(candidate_path).expanduser() != Path(shadow_path).expanduser()
        ):
            raise ValueError("candidate_policy and shadow candidate paths must match")
        if (self.shadow.enabled or self.canary.enabled) and not (candidate_path or shadow_path):
            raise ValueError("enabled shadow or canary requires a candidate policy source")
        if self.canary.enabled and (
            self.candidate_policy is None
            or self.candidate_policy.expected_policy_hash is None
        ):
            raise ValueError("enabled canary requires candidate_policy.expected_policy_hash")
        for segment in self.canary.segments:
            if segment.profile not in self.profiles:
                raise ValueError("canary segment profile must reference a declared profile")
        for alias, model in self.models.items():
            if model.provider not in self.providers:
                raise ValueError(f"model {alias!r} references an unknown provider")
            provider_protocol = (
                Protocol.OPENAI_RESPONSES
                if self.providers[model.provider].type == "openai"
                else Protocol.ANTHROPIC_MESSAGES
            )
            if model.protocol is not provider_protocol:
                raise ValueError(
                    f"model {alias!r} protocol {model.protocol.value!r} does not match provider "
                    f"{model.provider!r} type {self.providers[model.provider].type!r}"
                )
            if model.state_scope is not None and not model.state_scope.strip():
                raise ValueError(f"model {alias!r} state_scope must be non-empty when declared")
            if Capability.RESPONSE_STATE in model.capabilities and model.state_scope is None:
                raise ValueError(f"stateful model {alias!r} must declare state_scope")
        for name, profile in self.profiles.items():
            if isinstance(profile, AutoProfileConfig):
                continue
            if not profile.targets:
                raise ValueError(f"profile {name!r} must declare at least one protocol target chain")
            for protocol, target_chain in profile.targets.items():
                chain = (target_chain.primary, *target_chain.fallback)
                if len(chain) != len(set(chain)):
                    raise ValueError(f"profile {name!r} contains a fallback cycle or duplicate")
                unknown = [alias for alias in chain if alias not in self.models]
                if unknown:
                    raise ValueError(f"profile {name!r} references unknown models: {unknown}")
                configured_models = [self.models[alias] for alias in chain]
                if any(model.protocol is not protocol for model in configured_models):
                    raise ValueError(f"profile {name!r} mixes protocols in {protocol.value!r} target chain")
                primary = configured_models[0]
                for fallback_alias, fallback in zip(target_chain.fallback, configured_models[1:]):
                    if not primary.capabilities.issubset(fallback.capabilities):
                        raise ValueError(
                            f"fallback {fallback_alias!r} "
                            f"is not capability-equivalent to {target_chain.primary!r}"
                        )
                    if (
                        Capability.RESPONSE_STATE in primary.capabilities
                        and primary.state_scope != fallback.state_scope
                    ):
                        raise ValueError(
                            f"profile {name!r} fallback crosses state_scope for {protocol.value!r}"
                        )
        protocols = {model.protocol for model in self.models.values()}
        if not protocols:
            raise ValueError("automatic routing requires configured model targets")
        for protocol in protocols:
            tiers = {model.tier for model in self.models.values() if model.protocol is protocol}
            if set(Tier) - tiers:
                raise ValueError(f"automatic routing requires fast, balanced, and deep {protocol.value} targets")
        return self

    @property
    def config_hash(self) -> str:
        """Return a stable hash of the effective non-secret configuration."""

        effective = self.model_dump(mode="json")
        effective.pop("dashboard", None)
        canonical = json.dumps(effective, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:12]

    @property
    def policy_hash(self) -> str:
        """Return the legacy compatibility alias for ``config_hash``."""

        return self.config_hash

    @property
    def effective_policy_version(self) -> str:
        """Combine the human policy version with the effective configuration hash."""

        return f"{self.routing.policy_version}-{self.policy_hash}"

    def model_targets(self) -> dict[str, ModelTarget]:
        """Build the immutable model registry consumed by routing."""

        return {
            alias: ModelTarget(
                alias=alias,
                provider=model.provider,
                upstream_model=model.upstream_model,
                tier=model.tier,
                capabilities=model.capabilities,
                max_input_tokens=model.max_input_tokens,
                input_price_per_million=model.input_price_per_million,
                output_price_per_million=model.output_price_per_million,
                protocol=model.protocol,
                state_scope=model.state_scope,
            )
            for alias, model in self.models.items()
        }

    def resolve_secrets(self) -> tuple[str, dict[str, str]]:
        """Resolve required client and provider credentials without storing them in config."""

        client_key = _resolve_secret(self.server.client_api_key_env)
        if client_key is None:
            raise ValueError(f"missing required environment variable {self.server.client_api_key_env}")
        provider_keys: dict[str, str] = {}
        for name, provider in self.providers.items():
            key = _resolve_secret(provider.api_key_env)
            if key is None:
                raise ValueError(f"missing required environment variable {provider.api_key_env}")
            provider_keys[name] = key
        return client_key, provider_keys


def _resolve_secret(environment_name: str) -> str | None:
    """Read a secret and remove accidental copy/paste whitespace at its boundaries."""

    value = os.getenv(environment_name)
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def load_config(path: str | Path) -> RouterConfig:
    """Load and strictly validate one YAML configuration file."""

    config_path = Path(path).expanduser()
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict):
        raise TypeError("router configuration must be a YAML mapping")
    return RouterConfig.model_validate(raw)


def load_candidate_config(path: str | Path) -> RouterConfig:
    """Load a candidate policy while explicitly ignoring recursive shadow config."""

    config_path = Path(path).expanduser()
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict):
        raise TypeError("candidate configuration must be a YAML mapping")
    candidate = dict(raw)
    candidate.pop("candidate_policy", None)
    candidate.pop("shadow", None)
    candidate.pop("canary", None)
    return RouterConfig.model_validate(candidate)
