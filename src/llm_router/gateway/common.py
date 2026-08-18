"""Protocol-neutral HTTP gateway helpers."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from fastapi import Request

from llm_router.config import RouterConfig
from llm_router.domain import (
    ExecutionStats,
    OutcomeSignal,
    Protocol,
    ProtocolEnvelope,
    RouteEvent,
)
from llm_router.errors import RouterError, invalid_request
from llm_router.routing.feature_utils import summarize_features


async def read_json_object(request: Request, max_bytes: int) -> dict[str, Any]:
    """Read one bounded JSON object and reject upstream-selection fields."""

    content_length = request.headers.get("content-length")
    if content_length and (not content_length.isdigit() or int(content_length) > max_bytes):
        raise invalid_request("The request body exceeds the configured size limit.")
    body = await request.body()
    if len(body) > max_bytes:
        raise invalid_request("The request body exceeds the configured size limit.")
    try:
        value = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise invalid_request("The request body must be valid JSON.") from exc
    if not isinstance(value, dict):
        raise invalid_request("The request body must be a JSON object.")
    if isinstance(value.get("base_url"), str) or isinstance(value.get("provider"), str):
        raise invalid_request("The request cannot select an upstream URL or provider.")
    return value


def provider_extension_headers(config: RouterConfig, protocol: Protocol) -> tuple[str, ...]:
    """Return extension headers only for providers of the inbound protocol."""

    provider_type = "openai" if protocol is Protocol.OPENAI_RESPONSES else "anthropic"
    return tuple(
        header
        for provider in config.providers.values()
        if provider.type == provider_type
        for header in provider.extension_headers
    )


def route_headers(envelope: ProtocolEnvelope, plan: Any, response: Any) -> dict[str, str]:
    """Build stable router response headers from a resolved execution plan."""

    reason = plan.route_reason
    if plan.auxiliary_reasons:
        reason = ",".join((reason, *plan.auxiliary_reasons))
    return {
        "x-llm-router-request-id": envelope.request_id,
        "x-llm-router-profile": plan.profile,
        "x-llm-router-upstream-model": response.final_target.upstream_model,
        "x-llm-router-route-reason": reason[:256],
        "x-llm-router-policy-version": plan.policy_version,
        "x-llm-router-attempts": str(response.attempt_count),
    }


async def record_completion(
    runtime: Any,
    envelope: ProtocolEnvelope,
    routing_request: Any,
    plan: Any,
    response: Any,
    usage_extractor: Callable[[bytes], tuple[int | None, int | None]],
    task_id: str | None = None,
    session_key: str | None = None,
) -> None:
    """Record bounded completion telemetry and opt-in session state."""

    try:
        stats: ExecutionStats = await response.completion
        if isinstance(response.body, bytes):
            input_tokens, output_tokens = usage_extractor(response.body)
            stats = replace(stats, input_tokens=input_tokens, output_tokens=output_tokens)
        status = stats.status
        outcome = routing_request.outcome_signal
        if outcome is not OutcomeSignal.UNKNOWN and not envelope.endpoint.endswith("count_tokens"):
            runtime.sessions.record(session_key, response.final_target.tier, outcome)
        runtime.telemetry.record(
            RouteEvent(
                request_id=envelope.request_id,
                task_id=task_id,
                received_at=envelope.received_at,
                protocol=envelope.protocol.value,
                profile=plan.profile,
                stream=envelope.stream,
                feature_summary=summarize_features(routing_request),
                primary_model=plan.primary.alias,
                final_model=response.final_target.alias,
                route_reason=plan.route_reason,
                policy_version=plan.policy_version,
                status=status,
                attempt_count=response.attempt_count,
                time_to_first_event_ms=stats.time_to_first_event_ms,
                total_latency_ms=stats.total_latency_ms,
                input_tokens=stats.input_tokens,
                output_tokens=stats.output_tokens,
                error_code=stats.error_code,
                attempts=response.attempts,
                inbound_protocol=envelope.protocol.value,
                target_protocol=response.final_target.protocol.value,
                provider_account_scope=response.final_target.state_scope,
                response_state_requested=routing_request.response_state_requested,
                health_enabled=runtime.config.health.enabled,
                health_snapshot_revision=plan.health_snapshot_revision,
                health_filtered_count=plan.health_filtered_count,
                health_skipped_count=response.health_skipped_count,
                health_reason=plan.health_reason,
            )
        )
    except Exception:
        logging.getLogger("llm_router.gateway").exception(
            "completion telemetry failed",
            extra={"event": "completion_telemetry_failed", "request_id": envelope.request_id},
        )


def record_route_failure(
    runtime: Any,
    envelope: ProtocolEnvelope,
    routing_request: Any,
    availability: Any,
    error: RouterError,
    plan: Any | None = None,
    task_id: str | None = None,
) -> None:
    """Record a bounded no-available-target failure without session updates."""

    try:
        snapshot_revision = (
            error.health_snapshot_revision
            or (plan.health_snapshot_revision if plan is not None else getattr(availability, "revision", 0))
        )
        filtered_count = (
            error.health_filtered_count
            or (plan.health_filtered_count if plan is not None else 0)
        )
        health_reason = error.health_reason or "health_no_available_target"
        runtime.telemetry.record(
            RouteEvent(
                request_id=envelope.request_id,
                task_id=task_id,
                received_at=envelope.received_at,
                protocol=envelope.protocol.value,
                profile=routing_request.requested_profile,
                stream=envelope.stream,
                feature_summary=summarize_features(routing_request),
                primary_model="none",
                final_model="none",
                route_reason=health_reason,
                policy_version=runtime.config.effective_policy_version,
                status="error",
                attempt_count=0,
                total_latency_ms=(datetime.now(timezone.utc) - envelope.received_at).total_seconds()
                * 1000,
                error_code=error.code,
                attempts=error.health_skipped_attempts,
                inbound_protocol=envelope.protocol.value,
                response_state_requested=routing_request.response_state_requested,
                health_enabled=runtime.config.health.enabled,
                health_snapshot_revision=snapshot_revision,
                health_filtered_count=filtered_count,
                health_skipped_count=error.health_skipped_count,
                health_reason=health_reason,
            )
        )
    except Exception:
        logging.getLogger("llm_router.gateway").exception(
            "route failure telemetry failed",
            extra={"event": "route_failure_telemetry_failed", "request_id": envelope.request_id},
        )
