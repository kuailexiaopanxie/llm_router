"""W3C trace context, deterministic sampling, and fixed local span trees."""

from __future__ import annotations

import hashlib
import secrets
from decimal import Decimal

from llm_router.observability.models import (
    CostEstimate,
    RouteObservation,
    TraceContext,
    TraceSpan,
)


def _hex_id(bytes_count: int) -> str:
    """Generate a non-zero lowercase hexadecimal identifier."""

    value = secrets.token_hex(bytes_count)
    return value if set(value) != {"0"} else "1".zfill(bytes_count * 2)


def trace_context(traceparent: str | None, accept_remote: bool = True) -> TraceContext:
    """Parse a strict W3C version-00 parent or generate a local root."""

    if accept_remote and traceparent is not None:
        parts = traceparent.split("-")
        if (
            len(parts) == 4
            and parts[0] == "00"
            and len(parts[1]) == 32
            and len(parts[2]) == 16
            and len(parts[3]) == 2
            and all(character in "0123456789abcdef" for character in "".join(parts))
            and set(parts[1]) != {"0"}
            and set(parts[2]) != {"0"}
        ):
            return TraceContext(parts[1], _hex_id(8), parts[2], "remote_parent")
    return TraceContext(_hex_id(16), _hex_id(8), None, "generated")


def sampled(trace_id: str, rate: Decimal) -> bool:
    """Deterministically sample a trace ID using an exact threshold."""

    threshold = int(rate * 10_000)
    bucket = int.from_bytes(hashlib.sha256(trace_id.encode()).digest()[:8], "big") % 10_000
    return bucket < threshold


class TraceBuilder:
    """Construct a fixed whitelist-only span tree from terminal facts."""

    def __init__(self, sample_rate: Decimal, enabled: bool = True) -> None:
        """Bind immutable local sampling configuration."""

        self._rate = sample_rate
        self._enabled = enabled

    def build(
        self, observation: RouteObservation, cost: CostEstimate | None = None
    ) -> tuple[TraceSpan, ...]:
        """Build root, route, execute, and ordered attempt spans when sampled."""

        context = observation.trace_context
        if not self._enabled or not sampled(context.trace_id, self._rate):
            return ()
        request_duration = (observation.completed_at - observation.received_at).total_seconds() * 1000
        root_attributes: dict[str, str | int | float | bool] = {
            "llm_router.request_id": str(observation.request_id),
            "llm_router.endpoint_kind": observation.endpoint_kind.value,
            "llm_router.protocol": observation.protocol.value if observation.protocol else "unknown",
            "llm_router.profile": observation.profile or "unknown",
            "llm_router.terminal_stage": observation.terminal_stage.value,
            "llm_router.usage_status": observation.usage.status.value,
            "llm_router.cost_status": cost.status.value if cost else "unknown",
            "gen_ai.operation.name": "chat",
        }
        if observation.error_code is not None:
            root_attributes["llm_router.error_code"] = observation.error_code
        spans = [
            TraceSpan(
                context.trace_id,
                context.root_span_id,
                context.parent_span_id,
                observation.request_id,
                "llm_router.request",
                observation.received_at,
                request_duration,
                observation.status.value,
                root_attributes,
            )
        ]
        routing = observation.routing
        if routing is not None:
            route_id = _hex_id(8)
            spans.append(
                TraceSpan(
                    context.trace_id,
                    route_id,
                    context.root_span_id,
                    observation.request_id,
                    "llm_router.route",
                    routing.started_at,
                    routing.duration_ms,
                    routing.result,
                    {
                        "llm_router.policy_version": routing.policy_version,
                        "llm_router.policy_role": routing.policy_role,
                        "llm_router.route_reason": routing.route_reason or "none",
                    },
                )
            )
        execution = observation.execution
        if execution is not None:
            execute_id = _hex_id(8)
            spans.append(
                TraceSpan(
                    context.trace_id,
                    execute_id,
                    context.root_span_id,
                    observation.request_id,
                    "llm_router.execute",
                    execution.started_at,
                    execution.duration_ms,
                    execution.terminal_status,
                    {
                        "gen_ai.provider.name": execution.final_provider or "unknown",
                        "gen_ai.response.model": execution.final_target or "none",
                    },
                )
            )
            for attempt in execution.attempts:
                spans.append(
                    TraceSpan(
                        context.trace_id,
                        _hex_id(8),
                        execute_id,
                        observation.request_id,
                        "llm_router.provider.attempt",
                        attempt.started_at,
                        attempt.duration_ms,
                        attempt.status,
                        {
                            "gen_ai.provider.name": attempt.provider,
                            "gen_ai.request.model": attempt.model,
                            "llm_router.upstream_invoked": attempt.upstream_invoked,
                        },
                    )
                )
            if observation.stream and execution.committed:
                spans.append(
                    TraceSpan(
                        context.trace_id,
                        _hex_id(8),
                        execute_id,
                        observation.request_id,
                        "llm_router.stream",
                        execution.started_at,
                        execution.duration_ms,
                        execution.terminal_status,
                        {},
                    )
                )
        return tuple(spans)
