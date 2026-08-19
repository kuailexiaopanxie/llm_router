"""Strict configuration for local-first observability."""

from __future__ import annotations

import ipaddress
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ObservabilityStrictModel(BaseModel):
    """Reject unknown observability configuration keys."""

    model_config = ConfigDict(extra="forbid")


def decimal_rate(value: object, field: str) -> Decimal:
    """Parse one bounded non-negative decimal rate from a YAML string."""

    if not isinstance(value, str):
        raise TypeError(f"{field} must be a decimal string")
    try:
        rate = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{field} must be a decimal string") from exc
    if not rate.is_finite() or rate < 0 or rate > Decimal(1000000):
        raise ValueError(f"{field} is outside the supported range")
    digits = rate.as_tuple().digits
    exponent = rate.as_tuple().exponent
    if len(digits) > 18 or (isinstance(exponent, int) and exponent < -9):
        raise ValueError(f"{field} exceeds supported precision")
    return rate


class PricingConfig(ObservabilityStrictModel):
    """Declare versioned per-million rates for one model alias."""

    version: str = Field(min_length=1, max_length=128)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    input_per_million: Decimal | None = None
    output_per_million: Decimal | None = None
    cache_read_input_per_million: Decimal | None = None
    cache_write_input_per_million: Decimal | None = None

    @model_validator(mode="before")
    @classmethod
    def parse_rates(cls, value: object) -> object:
        """Require every configured monetary rate to originate as a string."""

        if not isinstance(value, dict):
            return value
        result = dict(value)
        for name in (
            "input_per_million",
            "output_per_million",
            "cache_read_input_per_million",
            "cache_write_input_per_million",
        ):
            if name in result and result[name] is not None:
                result[name] = decimal_rate(result[name], f"pricing.{name}")
        return result


class OtlpConfig(ObservabilityStrictModel):
    """Configure an optional bounded OTLP HTTP trace exporter."""

    enabled: bool = False
    endpoint: str = "http://127.0.0.1:4318/v1/traces"
    headers_env: str | None = Field(
        default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"
    )
    timeout_seconds: float = Field(default=3, gt=0, le=30)
    queue_capacity: int = Field(default=2048, ge=1, le=100_000)
    batch_size: int = Field(default=256, ge=1, le=10_000)
    allow_insecure: bool = False

    @model_validator(mode="after")
    def validate_endpoint(self) -> OtlpConfig:
        """Reject unsupported or remotely insecure OTLP endpoints."""

        parsed = urlparse(self.endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("observability tracing OTLP endpoint must use HTTP(S)")
        if parsed.scheme == "http":
            try:
                loopback = ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                loopback = parsed.hostname == "localhost"
            if not loopback and not self.allow_insecure:
                raise ValueError("non-loopback HTTP OTLP requires allow_insecure=true")
        if self.batch_size > self.queue_capacity:
            raise ValueError("OTLP batch_size cannot exceed queue_capacity")
        return self


class TracingConfig(ObservabilityStrictModel):
    """Configure deterministic local span capture and optional export."""

    enabled: bool = True
    sample_rate: Decimal = Decimal("1.0")
    accept_traceparent: bool = True
    local_store: bool = True
    otlp: OtlpConfig = Field(default_factory=OtlpConfig)

    @model_validator(mode="before")
    @classmethod
    def parse_sample_rate(cls, value: object) -> object:
        """Parse a decimal sample rate without binary floating-point input."""

        if not isinstance(value, dict) or "sample_rate" not in value:
            return value
        result = dict(value)
        raw = result["sample_rate"]
        if not isinstance(raw, (str, int, Decimal)) or isinstance(raw, bool):
            raise TypeError("tracing.sample_rate must be a decimal string or integer")
        rate = Decimal(str(raw))
        exponent = rate.as_tuple().exponent
        if not Decimal(0) <= rate <= Decimal(1):
            raise ValueError("tracing.sample_rate must be between 0 and 1")
        if isinstance(exponent, int) and exponent < -4:
            raise ValueError("tracing.sample_rate supports at most four decimal places")
        result["sample_rate"] = rate
        return result


class MetricsConfig(ObservabilityStrictModel):
    """Configure Prometheus exposure independently from durable capture."""

    enabled: bool = True
    require_auth: bool = False


class ObservabilityConfig(ObservabilityStrictModel):
    """Configure independent observation sinks and retention."""

    capture_enabled: bool = True
    queue_capacity: int = Field(default=2048, ge=1, le=100_000)
    tracing: TracingConfig = Field(default_factory=TracingConfig)
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)
    retention_days: int | None = Field(default=None, ge=1, le=3650)


def is_env_name(value: str) -> bool:
    """Return whether a string is a safe environment variable name."""

    return re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is not None
