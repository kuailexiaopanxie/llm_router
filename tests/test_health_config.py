"""Configuration validation for bounded health policy settings."""

from __future__ import annotations

from copy import deepcopy

import pytest
from conftest import router_config_data
from pydantic import ValidationError

from llm_router.config import RouterConfig


def test_missing_health_block_uses_v03_defaults() -> None:
    """Load v0.2 configuration without requiring a migration."""

    config = RouterConfig.model_validate(router_config_data())

    assert config.health.enabled is True
    assert config.health.failure_threshold == 2
    assert config.health.cooldown_seconds == 30
    assert config.health.max_cooldown_seconds == 300


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("failure_threshold", 0),
        ("failure_threshold", 11),
        ("failure_window_seconds", 9),
        ("cooldown_seconds", 0),
        ("max_cooldown_seconds", 86_401),
        ("backoff_multiplier", 8.1),
    ],
)
def test_health_bounds_reject_invalid_values(field: str, value: object) -> None:
    """Reject values outside the documented health policy bounds."""

    raw = router_config_data()
    raw["health"] = {field: value}

    with pytest.raises(ValidationError):
        RouterConfig.model_validate(raw)


def test_cooldown_order_and_unknown_fields_are_rejected() -> None:
    """Reject inverted cooldown bounds and unknown configuration keys."""

    inverted = router_config_data()
    inverted["health"] = {"cooldown_seconds": 60, "max_cooldown_seconds": 30}
    unknown = deepcopy(router_config_data())
    unknown["health"] = {"unexpected": True}

    with pytest.raises(ValidationError):
        RouterConfig.model_validate(inverted)
    with pytest.raises(ValidationError):
        RouterConfig.model_validate(unknown)


def test_health_configuration_changes_policy_hash() -> None:
    """Include the effective health policy in the deterministic hash."""

    enabled = RouterConfig.model_validate(router_config_data())
    raw = router_config_data()
    raw["health"] = {"enabled": False}
    disabled = RouterConfig.model_validate(raw)

    assert enabled.policy_hash != disabled.policy_hash
