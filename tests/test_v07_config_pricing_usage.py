"""v0.7 strict pricing and usage normalization tests."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from conftest import router_config_data
from pydantic import ValidationError

from llm_router.config import RouterConfig
from llm_router.domain import AttemptEvent, Protocol
from llm_router.observability.models import (
    CostStatus,
    EndpointKind,
    ExecutionObservation,
    RequestStatus,
    RouteObservation,
    TerminalStage,
    UsageStatus,
)
from llm_router.observability.pricing import CostCalculator, PricingCatalog
from llm_router.observability.tracing import trace_context
from llm_router.observability.usage import (
    normalize_anthropic_usage,
    normalize_openai_usage,
)
from llm_router.routing.policy import compile_routing_policy


def _priced_config() -> RouterConfig:
    """Build one config using exact versioned pricing strings."""

    data = router_config_data()
    data["models"]["anthropic_fast"]["pricing"] = {
        "version": "contract-1",
        "currency": "USD",
        "input_per_million": "3.00",
        "output_per_million": "15.00",
    }
    return RouterConfig.model_validate(data)


def test_pricing_requires_strings_and_does_not_change_policy_hash() -> None:
    """Reject YAML floats and exclude versioned pricing from routing identity."""

    base_data = router_config_data()
    priced_data = deepcopy(base_data)
    priced_data["models"]["anthropic_fast"]["pricing"] = {
        "version": "contract-1",
        "currency": "USD",
        "input_per_million": "3.00",
    }
    base = RouterConfig.model_validate(base_data)
    priced = RouterConfig.model_validate(priced_data)
    assert compile_routing_policy(base).routing_policy_hash == compile_routing_policy(
        priced
    ).routing_policy_hash

    priced_data["models"]["anthropic_fast"]["pricing"]["input_per_million"] = 3.0
    with pytest.raises((TypeError, ValidationError)):
        RouterConfig.model_validate(priced_data)


def test_remote_metrics_require_auth_and_example_config_is_valid() -> None:
    """Protect remote metrics while keeping the documented config loadable."""

    remote = router_config_data()
    remote["server"] = {"host": "0.0.0.0", "allow_remote_access": True}
    with pytest.raises(ValidationError):
        RouterConfig.model_validate(remote)
    remote["observability"] = {"metrics": {"require_auth": True}}
    assert RouterConfig.model_validate(remote).observability.metrics.require_auth

    import yaml

    example = Path(__file__).parents[1] / "router.example.yaml"
    assert RouterConfig.model_validate(yaml.safe_load(example.read_text())).version == 1


def test_decimal_cost_uses_nanos_and_marks_failed_attempt_unknown() -> None:
    """Calculate exact nanos while preserving a failed invoked attempt gap."""

    config = _priced_config()
    now = datetime.now(UTC)
    request_id = uuid4()
    usage = normalize_anthropic_usage(
        {"usage": {"input_tokens": 1000, "output_tokens": 10}}
    )
    execution = ExecutionObservation(
        now,
        1,
        None,
        "anthropic_fast",
        "anthropic",
        2,
        0,
        False,
        "success",
        (
            AttemptEvent(str(request_id), 1, "anthropic", "anthropic_deep", now, 0.5, "failed"),
            AttemptEvent(str(request_id), 2, "anthropic", "anthropic_fast", now, 0.5, "success"),
        ),
    )
    event = RouteObservation(
        request_id,
        None,
        trace_context(None),
        now,
        now,
        EndpointKind.MESSAGES,
        Protocol.ANTHROPIC_MESSAGES,
        "code/fast",
        False,
        TerminalStage.COMPLETED,
        RequestStatus.SUCCESS,
        None,
        execution,
        usage,
        None,
    )
    cost = CostCalculator(PricingCatalog.from_config(config)).estimate(event)
    assert cost.status is CostStatus.PARTIAL
    assert cost.known_amount_nanos == 3_150_000
    assert cost.unknown_invoked_attempts == 1
    assert Decimal(cost.line_items[0].rate_per_million).is_finite()


def test_protocol_usage_normalizers_reject_invalid_subsets() -> None:
    """Normalize cache and reasoning tokens without accepting invalid values."""

    anthropic = normalize_anthropic_usage(
        {
            "usage": {
                "input_tokens": 8,
                "cache_read_input_tokens": 2,
                "cache_creation_input_tokens": 1,
                "output_tokens": 4,
            }
        }
    )
    assert anthropic.status is UsageStatus.COMPLETE
    assert anthropic.input_cache_read_tokens == 2
    assert normalize_anthropic_usage(
        {"usage": {"input_tokens": 8, "output_tokens": 4}}
    ).status is UsageStatus.COMPLETE

    openai = normalize_openai_usage(
        {
            "usage": {
                "input_tokens": 10,
                "input_tokens_details": {"cached_tokens": 4},
                "output_tokens": 7,
                "output_tokens_details": {"reasoning_tokens": 3},
            }
        }
    )
    assert openai.input_uncached_tokens == 6
    assert openai.reasoning_output_tokens == 3
    assert normalize_openai_usage(
        {"usage": {"input_tokens": 1, "input_tokens_details": {"cached_tokens": 2}}}
    ).status is UsageStatus.INVALID
    assert normalize_anthropic_usage({"usage": {"input_tokens": True}}).status is UsageStatus.INVALID
