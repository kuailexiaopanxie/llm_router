"""Canonical, privacy-preserving codecs for persisted evaluation data."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from llm_router.domain import (
    Capability,
    ExecutionPlan,
    ExecutionTimeouts,
    ModelTarget,
    OutcomeSignal,
    Protocol,
    RoutingRequest,
    TaskSignals,
    Tier,
)
from llm_router.evaluation.models import (
    OutcomeEvent,
    RouteDecisionInput,
    RouterErrorSnapshot,
    RoutingPolicySnapshot,
)
from llm_router.health.models import (
    AvailabilityReason,
    AvailabilitySnapshot,
    FailureScope,
    HealthState,
    TargetAvailability,
)
from llm_router.routing.context import SessionSnapshot
from llm_router.routing.policy import (
    ProfilePolicy,
    RoutingPolicy,
    TargetChain,
    canonical_policy_json,
)

MAX_DECISION_INPUT_BYTES = 2 * 1024 * 1024
MAX_POLICY_SNAPSHOT_BYTES = 4 * 1024 * 1024


class CodecError(ValueError):
    """Report bounded incompatibility without exposing persisted content."""


def _check_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    """Reject missing or unknown persisted fields instead of guessing migrations."""

    if set(value) != expected:
        raise CodecError(f"{label} schema is incompatible")


def canonical_json(value: object) -> str:
    """Serialize a JSON value with deterministic separators and key order."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def utc_text(value: datetime | None) -> str | None:
    """Normalize an aware datetime to RFC3339 UTC."""

    if value is None:
        return None
    if value.tzinfo is None:
        raise CodecError("datetime must include a timezone")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_utc(value: object) -> datetime:
    """Parse one timezone-aware RFC3339 timestamp into UTC."""

    if not isinstance(value, str):
        raise CodecError("timestamp must be a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CodecError("timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise CodecError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _target(target: ModelTarget) -> dict[str, object]:
    """Encode one safe operational target identity."""

    return {
        "alias": target.alias,
        "provider": target.provider,
        "upstream_model": target.upstream_model,
        "tier": target.tier.value,
        "capabilities": sorted(item.value for item in target.capabilities),
        "max_input_tokens": target.max_input_tokens,
        "input_price_per_million": target.input_price_per_million,
        "output_price_per_million": target.output_price_per_million,
        "protocol": target.protocol.value,
        "state_scope": target.state_scope,
    }


def _decode_target(value: Mapping[str, Any]) -> ModelTarget:
    """Decode one operational target from trusted canonical fields."""

    return ModelTarget(
        alias=str(value["alias"]),
        provider=str(value["provider"]),
        upstream_model=str(value["upstream_model"]),
        tier=Tier(value["tier"]),
        capabilities=frozenset(Capability(item) for item in value["capabilities"]),
        max_input_tokens=int(value["max_input_tokens"]),
        input_price_per_million=value.get("input_price_per_million"),
        output_price_per_million=value.get("output_price_per_million"),
        protocol=Protocol(value["protocol"]),
        state_scope=value.get("state_scope"),
    )


def encode_routing_request(request: RoutingRequest) -> str:
    """Encode only bounded request features used by the kernel."""

    value = {
        "requested_profile": request.requested_profile,
        "required_capabilities": sorted(item.value for item in request.required_capabilities),
        "estimated_input_tokens": request.estimated_input_tokens,
        "message_count": request.message_count,
        "tool_rounds": request.tool_rounds,
        "system_size_bucket": request.system_size_bucket,
        "task_signals": {
            "complex_planning": request.task_signals.complex_planning,
            "debugging": request.task_signals.debugging,
            "review": request.task_signals.review,
            "multi_file_refactor": request.task_signals.multi_file_refactor,
        },
        "outcome_signal": request.outcome_signal.value,
        "stream": request.stream,
        "count_only": request.count_only,
        "protocol": request.protocol.value,
        "response_state_requested": request.response_state_requested,
        "provider_managed_tools_requested": request.provider_managed_tools_requested,
    }
    return canonical_json(value)


def decision_size_bytes(decision: RouteDecisionInput) -> int:
    """Measure the bounded serialized decision payload before queueing."""

    pieces = (
        encode_routing_request(decision.request),
        encode_session(decision.session) or "",
        encode_availability(decision.availability),
        encode_plan(decision.actual_plan) if decision.actual_plan else "",
        encode_error(decision.actual_error) if decision.actual_error else "",
    )
    return sum(len(piece.encode("utf-8")) for piece in pieces)


def decode_routing_request(payload: str) -> RoutingRequest:
    """Decode a sanitized request and reject incompatible shapes."""

    try:
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise CodecError("routing request is incompatible")
        _check_keys(
            value,
            {
                "requested_profile",
                "required_capabilities",
                "estimated_input_tokens",
                "message_count",
                "tool_rounds",
                "system_size_bucket",
                "task_signals",
                "outcome_signal",
                "stream",
                "count_only",
                "protocol",
                "response_state_requested",
                "provider_managed_tools_requested",
            },
            "routing request",
        )
        signals = value["task_signals"]
        if not isinstance(signals, dict):
            raise CodecError("routing request is incompatible")
        _check_keys(signals, {"complex_planning", "debugging", "review", "multi_file_refactor"}, "task signals")
        return RoutingRequest(
            requested_profile=value["requested_profile"],
            required_capabilities=frozenset(Capability(item) for item in value["required_capabilities"]),
            estimated_input_tokens=value["estimated_input_tokens"],
            message_count=value["message_count"],
            tool_rounds=value["tool_rounds"],
            system_size_bucket=value["system_size_bucket"],
            task_signals=TaskSignals(**signals),
            outcome_signal=OutcomeSignal(value["outcome_signal"]),
            stream=value["stream"],
            count_only=value["count_only"],
            protocol=Protocol(value["protocol"]),
            response_state_requested=value["response_state_requested"],
            provider_managed_tools_requested=value["provider_managed_tools_requested"],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CodecError("routing request is incompatible") from exc


def encode_session(session: SessionSnapshot | None) -> str | None:
    """Encode an optional session snapshot without an identifier."""

    if session is None:
        return None
    return canonical_json(
        {
            "last_tier": session.last_tier.value,
            "last_outcome": session.last_outcome.value,
            "consecutive_failures": session.consecutive_failures,
            "requests_since_failure": session.requests_since_failure,
        }
    )


def decode_session(payload: str | None) -> SessionSnapshot | None:
    """Decode an optional session snapshot."""

    if payload is None:
        return None
    try:
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise CodecError("session snapshot is incompatible")
        _check_keys(
            value,
            {"last_tier", "last_outcome", "consecutive_failures", "requests_since_failure"},
            "session snapshot",
        )
        return SessionSnapshot(
            Tier(value["last_tier"]),
            OutcomeSignal(value["last_outcome"]),
            int(value["consecutive_failures"]),
            int(value["requests_since_failure"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CodecError("session snapshot is incompatible") from exc


def encode_availability(snapshot: AvailabilitySnapshot) -> str:
    """Encode bounded health facts for deterministic replay."""

    return canonical_json(
        {
            "revision": snapshot.revision,
            "observed_at": utc_text(snapshot.observed_at),
            "earliest_recovery_at": utc_text(snapshot.earliest_recovery_at),
            "target_states": {
                alias: {
                    "eligible": state.eligible,
                    "state": state.state.value,
                    "reason": state.reason.value,
                    "retry_at": utc_text(state.retry_at),
                    "probe_scope": state.probe_scope.value if state.probe_scope else None,
                    "probe_key": state.probe_key,
                }
                for alias, state in sorted(snapshot.target_states.items())
            },
        }
    )


def decode_availability(payload: str) -> AvailabilitySnapshot:
    """Decode an immutable availability snapshot."""

    try:
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise CodecError("availability snapshot is incompatible")
        _check_keys(value, {"revision", "observed_at", "earliest_recovery_at", "target_states"}, "availability snapshot")
        states = {
            alias: TargetAvailability(
                eligible=item["eligible"],
                state=HealthState(item["state"]),
                reason=AvailabilityReason(item["reason"]),
                retry_at=parse_utc(item["retry_at"]) if item["retry_at"] else None,
                probe_scope=FailureScope(item["probe_scope"]) if item["probe_scope"] else None,
                probe_key=item["probe_key"],
            )
            for alias, item in value["target_states"].items()
        }
        return AvailabilitySnapshot(
            int(value["revision"]),
            parse_utc(value["observed_at"]),
            MappingProxyType(states),
            parse_utc(value["earliest_recovery_at"]) if value["earliest_recovery_at"] else None,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CodecError("availability snapshot is incompatible") from exc


def encode_plan(plan: ExecutionPlan) -> str:
    """Encode the complete bounded execution shape used for comparison."""

    return canonical_json(
        {
            "primary": _target(plan.primary),
            "fallbacks": [_target(item) for item in plan.fallbacks],
            "attempt_limit": plan.attempt_limit,
            "timeouts": {
                "connect_seconds": plan.timeouts.connect_seconds,
                "response_header_seconds": plan.timeouts.response_header_seconds,
                "non_stream_deadline_seconds": plan.timeouts.non_stream_deadline_seconds,
                "stream_idle_seconds": plan.timeouts.stream_idle_seconds,
                "stream_max_seconds": plan.timeouts.stream_max_seconds,
            },
            "route_reason": plan.route_reason,
            "auxiliary_reasons": list(plan.auxiliary_reasons),
            "profile": plan.profile,
            "policy_version": plan.policy_version,
            "health_snapshot_revision": plan.health_snapshot_revision,
            "health_filtered_count": plan.health_filtered_count,
            "health_reason": plan.health_reason,
        }
    )


def decode_plan(payload: str) -> ExecutionPlan:
    """Decode a normalized execution plan."""

    try:
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise CodecError("execution plan is incompatible")
        _check_keys(
            value,
            {
                "primary",
                "fallbacks",
                "attempt_limit",
                "timeouts",
                "route_reason",
                "auxiliary_reasons",
                "profile",
                "policy_version",
                "health_snapshot_revision",
                "health_filtered_count",
                "health_reason",
            },
            "execution plan",
        )
        return ExecutionPlan(
            primary=_decode_target(value["primary"]),
            fallbacks=tuple(_decode_target(item) for item in value["fallbacks"]),
            attempt_limit=int(value["attempt_limit"]),
            timeouts=ExecutionTimeouts(**value["timeouts"]),
            route_reason=value["route_reason"],
            auxiliary_reasons=tuple(value["auxiliary_reasons"]),
            profile=value["profile"],
            policy_version=value["policy_version"],
            health_snapshot_revision=int(value["health_snapshot_revision"]),
            health_filtered_count=int(value["health_filtered_count"]),
            health_reason=value["health_reason"],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CodecError("execution plan is incompatible") from exc


def encode_error(error: RouterErrorSnapshot) -> str:
    """Encode a bounded RouterError snapshot without its message."""

    return canonical_json(
        {
            "code": error.code,
            "http_status": error.http_status,
            "fallback_allowed": error.fallback_allowed,
            "retry_after": error.retry_after,
            "health_snapshot_revision": error.health_snapshot_revision,
            "health_filtered_count": error.health_filtered_count,
            "health_reason": error.health_reason,
        }
    )


def decode_error(payload: str) -> RouterErrorSnapshot:
    """Decode one bounded RouterError snapshot."""

    try:
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise CodecError("router error is incompatible")
        _check_keys(
            value,
            {
                "code",
                "http_status",
                "fallback_allowed",
                "retry_after",
                "health_snapshot_revision",
                "health_filtered_count",
                "health_reason",
            },
            "router error",
        )
        return RouterErrorSnapshot(**value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CodecError("router error is incompatible") from exc


def outcome_payload_hash(event: OutcomeEvent) -> str:
    """Hash all client semantic fields and exclude server receipt time."""

    payload = canonical_json(
        {
            "event_id": str(event.event_id),
            "request_id": str(event.request_id),
            "task_id": str(event.task_id) if event.task_id else None,
            "verdict": event.verdict.value,
            "evidence": event.evidence.value,
            "source": event.source.value,
            "observed_at": utc_text(event.observed_at),
        }
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def make_policy_snapshot(config: Any, created_at: datetime) -> RoutingPolicySnapshot:
    """Create a stable policy snapshot from validated RouterConfig."""

    payload = canonical_policy_json(config)
    if len(payload.encode()) > MAX_POLICY_SNAPSHOT_BYTES:
        raise CodecError("routing policy snapshot exceeds the size limit")
    digest = hashlib.sha256(payload.encode()).hexdigest()
    return RoutingPolicySnapshot(
        routing_policy_hash=digest,
        policy_version=config.routing.policy_version,
        routing_algorithm_version="v1",
        policy_json=payload,
        created_at=created_at,
    )


def decode_policy_snapshot(snapshot: RoutingPolicySnapshot) -> RoutingPolicy:
    """Compile one persisted canonical policy without config secrets."""

    digest = hashlib.sha256(snapshot.policy_json.encode()).hexdigest()
    if digest != snapshot.routing_policy_hash:
        raise CodecError("routing policy snapshot hash is invalid")
    try:
        value = json.loads(snapshot.policy_json)
        targets = {
            alias: _decode_target({"alias": alias, **item})
            for alias, item in value["targets"].items()
        }
        profiles: dict[str, ProfilePolicy] = {}
        for name, item in value["profiles"].items():
            chains = {
                Protocol(protocol): TargetChain(chain["primary"], tuple(chain["fallback"]))
                for protocol, chain in item["targets"].items()
            }
            profiles[name] = ProfilePolicy(item["mode"] == "auto", MappingProxyType(chains))
        return RoutingPolicy(
            targets=MappingProxyType(targets),
            profiles=MappingProxyType(profiles),
            default_profile=value["default_profile"],
            policy_version=value["policy_version"],
            failure_escalation_requests=int(value["failure_escalation_requests"]),
            fast_max_input_tokens=int(value["fast_max_input_tokens"]),
            balanced_max_input_tokens=int(value["balanced_max_input_tokens"]),
            deep_tool_rounds_threshold=int(value["deep_tool_rounds_threshold"]),
            attempt_limit=int(value["attempt_limit"]),
            timeouts=ExecutionTimeouts(**value["timeouts"]),
            routing_policy_hash=snapshot.routing_policy_hash,
            schema_version=int(value["schema_version"]),
            routing_algorithm_version=value["routing_algorithm_version"],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CodecError("routing policy snapshot is incompatible") from exc
