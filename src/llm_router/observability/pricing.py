"""Versioned pricing catalog and exact known-cost calculation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from types import MappingProxyType

from llm_router.config import RouterConfig
from llm_router.observability.models import (
    CostEstimate,
    CostLineItem,
    CostStatus,
    RouteObservation,
    UsageStatus,
)


@dataclass(frozen=True, slots=True)
class ModelPricing:
    """Store one immutable versioned pricing snapshot."""

    alias: str
    version: str
    currency: str
    pricing_id: str
    rates: Mapping[str, Decimal]


class PricingCatalog:
    """Map configured model aliases to immutable pricing snapshots."""

    def __init__(self, entries: Mapping[str, ModelPricing]) -> None:
        """Freeze compiled pricing entries."""

        self.entries = MappingProxyType(dict(entries))

    @classmethod
    def from_config(cls, config: RouterConfig) -> PricingCatalog:
        """Compile versioned and deprecated legacy pricing declarations."""

        entries: dict[str, ModelPricing] = {}
        for alias, model in config.models.items():
            if model.pricing is not None:
                rates = {
                    kind: rate
                    for kind, rate in {
                        "input_uncached": model.pricing.input_per_million,
                        "output": model.pricing.output_per_million,
                        "input_cache_read": model.pricing.cache_read_input_per_million,
                        "input_cache_write": model.pricing.cache_write_input_per_million,
                    }.items()
                    if rate is not None
                }
                entries[alias] = _pricing(alias, model.pricing.version, model.pricing.currency, rates)
            elif model.input_price_per_million is not None or model.output_price_per_million is not None:
                rates = {}
                if model.input_price_per_million is not None:
                    rates["input_uncached"] = Decimal(str(model.input_price_per_million))
                if model.output_price_per_million is not None:
                    rates["output"] = Decimal(str(model.output_price_per_million))
                entries[alias] = _pricing(alias, "legacy", "USD", rates)
        return cls(entries)


def _pricing(
    alias: str, version: str, currency: str, rates: Mapping[str, Decimal]
) -> ModelPricing:
    """Build one canonical pricing identity."""

    document = {
        "alias": alias,
        "version": version,
        "currency": currency,
        "rates": {key: format(value, "f") for key, value in sorted(rates.items())},
    }
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return ModelPricing(
        alias,
        version,
        currency,
        hashlib.sha256(encoded.encode()).hexdigest(),
        MappingProxyType(dict(rates)),
    )


class CostCalculator:
    """Calculate only the auditable known portion of estimated cost."""

    def __init__(self, catalog: PricingCatalog) -> None:
        """Bind an immutable startup pricing catalog."""

        self._catalog = catalog

    def estimate(self, observation: RouteObservation) -> CostEstimate:
        """Estimate known nanos without treating missing facts as zero."""

        usage = observation.usage
        execution = observation.execution
        if (
            usage.status is UsageStatus.NOT_APPLICABLE
            or execution is None
            or not any(attempt.upstream_invoked for attempt in execution.attempts)
        ):
            return CostEstimate(CostStatus.NOT_APPLICABLE, None, None, None)
        if usage.status in {UsageStatus.MISSING, UsageStatus.INVALID}:
            return CostEstimate(CostStatus.USAGE_MISSING, None, None, None)
        target = execution.final_target
        pricing = self._catalog.entries.get(target or "")
        unknown_attempts = sum(
            attempt.upstream_invoked and attempt.status not in {"success", "committed"}
            for attempt in execution.attempts
        )
        if pricing is None:
            return CostEstimate(
                CostStatus.UNPRICED,
                None,
                None,
                None,
                unknown_invoked_attempts=unknown_attempts,
            )
        usage_values = {
            "input_uncached": usage.input_uncached_tokens,
            "input_cache_read": usage.input_cache_read_tokens,
            "input_cache_write": usage.input_cache_write_tokens,
            "output": usage.output_tokens,
        }
        lines: list[CostLineItem] = []
        unknown: list[str] = []
        for kind, tokens in usage_values.items():
            if tokens is None:
                continue
            rate = pricing.rates.get(kind)
            if rate is None:
                unknown.append(kind)
                continue
            nanos = int((Decimal(tokens) * rate * Decimal(1000)).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
            lines.append(CostLineItem(kind, tokens, format(rate, "f"), nanos))
        amount = sum(line.amount_nanos for line in lines) if lines else None
        incomplete = bool(unknown or unknown_attempts or usage.status is UsageStatus.PARTIAL)
        status = CostStatus.PARTIAL if incomplete and amount is not None else CostStatus.COMPLETE
        if amount is None:
            status = CostStatus.UNPRICED
        return CostEstimate(
            status,
            pricing.currency,
            pricing.pricing_id,
            amount,
            tuple(lines),
            tuple(unknown),
            unknown_attempts,
        )
