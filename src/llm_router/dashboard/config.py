"""Strict configuration for the local dashboard."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DashboardConfig(BaseModel):
    """Configure the disabled-by-default local read-only dashboard."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    require_auth: bool = True
    default_range_hours: int = Field(default=24, ge=1, le=168)
    max_range_days: int = Field(default=90, ge=1, le=365)
    refresh_seconds: int = Field(default=15, ge=5, le=300)
    query_timeout_ms: int = Field(default=2000, ge=100, le=10_000)

    @model_validator(mode="after")
    def validate_range(self) -> DashboardConfig:
        """Ensure the configured maximum contains the default range."""

        if self.default_range_hours > self.max_range_days * 24:
            raise ValueError("dashboard max_range_days must cover default_range_hours")
        return self

