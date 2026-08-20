"""Dashboard configuration and cross-validation tests."""

from __future__ import annotations

import pytest
from conftest import router_config_data

from llm_router.config import RouterConfig


def test_dashboard_defaults_disabled_and_hash_compatible() -> None:
    """Old configuration receives a disabled dashboard without hash drift."""

    data = router_config_data()
    first = RouterConfig.model_validate(data)
    data["dashboard"] = {"enabled": False}
    second = RouterConfig.model_validate(data)
    assert first.dashboard.enabled is False
    assert first.config_hash == second.config_hash


def test_remote_unauthenticated_dashboard_is_rejected() -> None:
    """Remote admin exposure requires the existing client-key auth boundary."""

    data = router_config_data()
    data["server"] = {"host": "0.0.0.0", "allow_remote_access": True}
    data["observability"] = {"metrics": {"require_auth": True}}
    data["dashboard"] = {"enabled": True, "require_auth": False}
    with pytest.raises(ValueError, match="remote dashboard"):
        RouterConfig.model_validate(data)


@pytest.mark.parametrize("field,value", [("default_range_hours", 0), ("refresh_seconds", 4), ("query_timeout_ms", 99)])
def test_dashboard_bounds_are_strict(field: str, value: int) -> None:
    """Reject values outside the documented operational bounds."""

    data = router_config_data()
    data["dashboard"] = {field: value}
    with pytest.raises(ValueError):
        RouterConfig.model_validate(data)
