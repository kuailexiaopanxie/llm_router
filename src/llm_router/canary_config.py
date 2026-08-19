"""Strict configuration values for controlled canary routing."""

from __future__ import annotations

import re
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from llm_router.domain import Protocol

_HASH_PATTERN = r"^[0-9a-f]{64}$"
_ENV_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"
_PROFILE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$"


class CanaryStrictModel(BaseModel):
    """Reject unknown canary configuration fields."""

    model_config = ConfigDict(extra="forbid")


class CandidatePolicyConfig(CanaryStrictModel):
    """Identify one local candidate policy and its approved hash."""

    config_path: str
    expected_policy_hash: str | None = Field(default=None, pattern=_HASH_PATTERN)

    @model_validator(mode="after")
    def validate_path(self) -> CandidatePolicyConfig:
        """Require a plain local file path without shell or URL syntax."""

        path = self.config_path
        forbidden = ("://", "$", "`", ";", "&&", "||", "|", "\n", "\r", "\0")
        if not path or path != path.strip() or any(token in path for token in forbidden):
            raise ValueError("candidate_policy.config_path must be a local file path")
        return self


class CanarySegmentConfig(CanaryStrictModel):
    """Declare one protocol and effective profile eligible for Canary."""

    protocol: Protocol
    profile: str = Field(pattern=_PROFILE_PATTERN)


class CanaryConfig(CanaryStrictModel):
    """Bound controlled Canary assignment and startup gate settings."""

    enabled: bool = False
    traffic_rate: Decimal = Decimal("0.01")
    assignment_salt_env: str = Field(
        default="LLM_ROUTER_CANARY_SALT", pattern=_ENV_PATTERN
    )
    segments: tuple[CanarySegmentConfig, ...] = ()
    minimum_shadow_evaluated: int = Field(default=100, ge=1, le=100_000)
    shadow_gate_lookback_seconds: int = Field(default=86_400, ge=3_600, le=604_800)

    @model_validator(mode="after")
    def validate_canary(self) -> CanaryConfig:
        """Enforce bounded rates and unique explicit segments when enabled."""

        exponent = self.traffic_rate.as_tuple().exponent
        if isinstance(exponent, int) and exponent < -4:
            raise ValueError("canary.traffic_rate supports at most four decimal places")
        if self.enabled and not Decimal("0.0001") <= self.traffic_rate <= Decimal("0.25"):
            raise ValueError("enabled canary.traffic_rate must be between 0.0001 and 0.25")
        if not self.enabled and not Decimal(0) <= self.traffic_rate <= Decimal("0.25"):
            raise ValueError("canary.traffic_rate must be between 0 and 0.25")
        if self.enabled and not 1 <= len(self.segments) <= 32:
            raise ValueError("enabled canary requires between 1 and 32 segments")
        pairs = {(segment.protocol, segment.profile) for segment in self.segments}
        if len(pairs) != len(self.segments):
            raise ValueError("canary segments must be unique")
        if not re.fullmatch(_ENV_PATTERN, self.assignment_salt_env):
            raise ValueError("canary.assignment_salt_env is invalid")
        return self

    @property
    def threshold(self) -> int:
        """Convert the exact traffic rate to a 0..2500 bucket threshold."""

        return int(self.traffic_rate * 10_000)
