"""Strict YAML configuration loading and startup validation."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from llm_router.domain import Capability, ExecutionTimeouts, ModelTarget, Tier

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


class ProviderConfig(StrictModel):
    """Anthropic-compatible upstream connection settings."""

    type: Literal["anthropic"]
    base_url: str
    api_key_env: str
    auth_scheme: Literal["x_api_key", "bearer"] = "x_api_key"
    max_concurrency: int = Field(default=16, ge=1, le=1024)
    extension_headers: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_base_url(self) -> ProviderConfig:
        """Allow only fixed HTTP(S) upstream origins from configuration."""

        if not self.base_url.startswith(("https://", "http://")):
            raise ValueError("base_url must use http or https")
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

    @model_validator(mode="after")
    def validate_model_identity(self) -> ModelConfig:
        """Reject empty upstream identifiers that would produce ambiguous requests."""

        if not self.provider.strip() or not self.upstream_model.strip():
            raise ValueError("model provider and upstream_model must be non-empty")
        return self


class AutoProfileConfig(StrictModel):
    """Marker for the deterministic automatic policy."""

    mode: Literal["auto"]


class ExplicitProfileConfig(StrictModel):
    """Ordered model aliases for an explicit route profile."""

    primary: str
    fallback: tuple[str, ...] = ()


ProfileConfig = Annotated[AutoProfileConfig | ExplicitProfileConfig, Field(union_mode="left_to_right")]


class RoutingConfig(StrictModel):
    """Versioned deterministic policy thresholds."""

    default_profile: str = "code/auto"
    policy_version: str = "v1"
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
    providers: dict[str, ProviderConfig]
    models: dict[str, ModelConfig]
    profiles: dict[str, ProfileConfig]
    routing: RoutingConfig
    timeouts: TimeoutConfig

    @model_validator(mode="after")
    def validate_references(self) -> RouterConfig:
        """Validate provider, profile, capability, and fallback graph integrity."""

        alias_pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
        invalid_aliases = [alias for alias in self.models if not alias_pattern.fullmatch(alias)]
        if invalid_aliases:
            raise ValueError(f"invalid model alias: {invalid_aliases[0]!r}")
        invalid_profiles = [name for name in self.profiles if not alias_pattern.fullmatch(name)]
        if invalid_profiles:
            raise ValueError(f"invalid profile name: {invalid_profiles[0]!r}")
        if self.routing.default_profile not in self.profiles:
            raise ValueError("routing.default_profile must reference a declared profile")
        for alias, model in self.models.items():
            if model.provider not in self.providers:
                raise ValueError(f"model {alias!r} references an unknown provider")
        for name, profile in self.profiles.items():
            if isinstance(profile, AutoProfileConfig):
                continue
            chain = (profile.primary, *profile.fallback)
            if len(chain) != len(set(chain)):
                raise ValueError(f"profile {name!r} contains a fallback cycle or duplicate")
            unknown = [alias for alias in chain if alias not in self.models]
            if unknown:
                raise ValueError(f"profile {name!r} references unknown models: {unknown}")
            primary = self.models[profile.primary]
            for fallback_alias in profile.fallback:
                fallback = self.models[fallback_alias]
                if not primary.capabilities.issubset(fallback.capabilities):
                    raise ValueError(
                        f"fallback {fallback_alias!r} is not capability-equivalent to {profile.primary!r}"
                    )
        tiers = {model.tier for model in self.models.values()}
        if set(Tier) - tiers:
            raise ValueError("automatic routing requires at least one model in every tier")
        return self

    @property
    def policy_hash(self) -> str:
        """Return a stable hash of the effective non-secret configuration."""

        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:12]

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
        raise ValueError("router configuration must be a YAML mapping")
    return RouterConfig.model_validate(raw)
