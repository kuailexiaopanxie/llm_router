"""Protocol-neutral HTTP gateway helpers."""

from __future__ import annotations

import json
import logging
from datetime import UTC
from typing import Any

from fastapi import Request

from llm_router.config import RouterConfig
from llm_router.domain import (
    ExecutionStats,
    OutcomeSignal,
    Protocol,
    ProtocolEnvelope,
)
from llm_router.errors import RouterError, invalid_request
from llm_router.observability.lifecycle import RequestObservation
from llm_router.observability.models import (
    ExecutionObservation,
    RequestStatus,
    RoutingObservation,
    TerminalStage,
    UsageBreakdown,
)
from llm_router.routing.coordinator import RoutingResolution


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


def route_headers(
    envelope: ProtocolEnvelope, plan: Any, response: Any, trace_id: str
) -> dict[str, str]:
    """Build stable router response headers from a resolved execution plan."""

    reason = plan.route_reason
    if plan.auxiliary_reasons:
        reason = ",".join((reason, *plan.auxiliary_reasons))
    return {
        "x-llm-router-request-id": envelope.request_id,
        "x-llm-router-trace-id": trace_id,
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
    response: Any,
    lifecycle: RequestObservation,
    session_key: str | None = None,
) -> None:
    """Apply session outcome and finish one execution lifecycle."""

    try:
        stats: ExecutionStats = await response.completion
    except Exception:  # noqa: BLE001 - completion futures are an isolation boundary.
        logging.getLogger("llm_router.gateway").error(
            "Response completion tracking failed",
            extra={"event": "completion_tracking_failed", "request_id": envelope.request_id},
        )
        lifecycle.finish(
            RequestStatus.ERROR,
            TerminalStage.EXECUTION_POST_COMMIT if envelope.stream else TerminalStage.EXECUTION_PRE_COMMIT,
            "router_internal_error",
        )
        return
    outcome = routing_request.outcome_signal
    if outcome is not OutcomeSignal.UNKNOWN and not envelope.endpoint.endswith("count_tokens"):
        try:
            runtime.sessions.record(session_key, response.final_target.tier, outcome)
        except Exception:  # noqa: BLE001 - session state must not block observations.
            logging.getLogger("llm_router.gateway").error(
                "Session outcome update failed",
                extra={"event": "session_outcome_failed", "request_id": envelope.request_id},
            )
    try:
        usage = stats.usage or (
            UsageBreakdown.not_applicable()
            if envelope.endpoint.endswith("count_tokens")
            else UsageBreakdown.missing()
        )
        lifecycle.executing(
            ExecutionObservation(
                response.attempts[0].started_at if response.attempts else envelope.received_at,
                stats.total_latency_ms,
                stats.time_to_first_event_ms,
                response.final_target.alias,
                response.final_target.provider,
                response.attempt_count,
                response.health_skipped_count,
                True,
                stats.status,
                response.attempts,
            ),
            usage,
        )
        status = RequestStatus(stats.status)
        stage = (
            TerminalStage.COMPLETED
            if status is RequestStatus.SUCCESS
            else TerminalStage.EXECUTION_POST_COMMIT
            if envelope.stream
            else TerminalStage.EXECUTION_PRE_COMMIT
        )
        lifecycle.finish(status, stage, stats.error_code)
    except Exception:  # noqa: BLE001 - completion observation must remain fail-open.
        logging.getLogger("llm_router.gateway").error(
            "Request observation completion failed",
            extra={"event": "completion_observation_failed", "request_id": envelope.request_id},
        )
        lifecycle.finish(
            RequestStatus.ERROR,
            (
                TerminalStage.EXECUTION_POST_COMMIT
                if envelope.stream
                else TerminalStage.EXECUTION_PRE_COMMIT
            ),
            "router_internal_error",
        )


def routing_facts(
    resolution: RoutingResolution, requested_profile: str
) -> RoutingObservation:
    """Convert one actual resolution into bounded routing observation facts."""

    assignment = resolution.assignment
    plan = resolution.plan
    error = resolution.error
    return RoutingObservation(
        requested_profile=requested_profile,
        effective_profile=plan.profile if plan else None,
        started_at=resolution.started_at,
        duration_ms=resolution.duration_ms,
        primary_model=plan.primary.alias if plan else None,
        target_aliases=tuple(target.alias for target in plan.targets) if plan else (),
        policy_version=resolution.policy_version,
        policy_hash=resolution.routing_policy_hash,
        policy_role=assignment.role.value if assignment else "control",
        assignment_reason=assignment.reason.value if assignment else None,
        route_reason=plan.route_reason if plan else error.health_reason if error else None,
        auxiliary_reasons=plan.auxiliary_reasons if plan else (),
        health_snapshot_revision=(
            plan.health_snapshot_revision
            if plan
            else error.health_snapshot_revision
            if error
            else 0
        ),
        health_filtered_count=(
            plan.health_filtered_count
            if plan
            else error.health_filtered_count
            if error
            else 0
        ),
        health_reason=plan.health_reason if plan else error.health_reason if error else None,
        result="plan" if plan else "error",
    )


def execution_failure_facts(error: RouterError) -> ExecutionObservation | None:
    """Convert an attached bounded execution failure snapshot."""

    failure = error.execution
    if failure is None:
        return None
    final = next(
        (attempt for attempt in reversed(failure.attempts) if attempt.upstream_invoked),
        None,
    )
    return ExecutionObservation(
        failure.started_at.astimezone(UTC),
        failure.duration_ms,
        None,
        final.model if final else None,
        final.provider if final else None,
        failure.upstream_attempt_count,
        failure.health_skipped_count,
        failure.committed,
        "error",
        failure.attempts,
    )


def finish_error(
    lifecycle: RequestObservation,
    error: RouterError,
    stage: TerminalStage,
) -> None:
    """Finish one expected error with attached execution facts when present."""

    execution = execution_failure_facts(error)
    if execution is not None:
        lifecycle.executing(execution, UsageBreakdown.missing())
        stage = TerminalStage.EXECUTION_PRE_COMMIT
    status = (
        RequestStatus.CANCELLED
        if error.code == "router_cancelled"
        else RequestStatus.ERROR
    )
    lifecycle.finish(status, stage, error.code)
