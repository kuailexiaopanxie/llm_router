"""Canonical codec for bounded online shadow comparisons."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from llm_router.domain import Protocol
from llm_router.evaluation.codec import (
    CodecError,
    canonical_json,
    decode_error,
    decode_plan,
    encode_error,
    encode_plan,
    parse_utc,
    utc_text,
)
from llm_router.evaluation.models import (
    ReplayChange,
    ShadowDecision,
    ShadowReason,
    ShadowStatus,
)

MAX_SHADOW_DECISION_BYTES = 64 * 1024


def encode_shadow_decision(decision: ShadowDecision) -> str:
    """Encode one shadow decision using only approved bounded fields."""

    return canonical_json(
        {
            "request_id": str(decision.request_id),
            "recorded_at": utc_text(decision.recorded_at),
            "evaluated_at": utc_text(decision.evaluated_at),
            "protocol": decision.protocol.value,
            "requested_profile": decision.requested_profile,
            "actual_policy_hash": decision.actual_policy_hash,
            "candidate_policy_hash": decision.candidate_policy_hash,
            "candidate_algorithm_version": decision.candidate_algorithm_version,
            "schema_version": decision.schema_version,
            "status": decision.status.value,
            "change": decision.change.value if decision.change else None,
            "reason": decision.reason.value if decision.reason else None,
            "actual_plan": encode_plan(decision.actual_plan) if decision.actual_plan else None,
            "actual_error": encode_error(decision.actual_error) if decision.actual_error else None,
            "candidate_plan": (
                encode_plan(decision.candidate_plan) if decision.candidate_plan else None
            ),
            "candidate_error": (
                encode_error(decision.candidate_error) if decision.candidate_error else None
            ),
        }
    )


def shadow_payload_hash(decision: ShadowDecision) -> str:
    """Hash the complete canonical shadow payload for idempotency checks."""

    return hashlib.sha256(encode_shadow_decision(decision).encode()).hexdigest()


def validate_shadow_size(decision: ShadowDecision) -> None:
    """Reject a comparison that exceeds the fixed persistence budget."""

    if len(encode_shadow_decision(decision).encode()) > MAX_SHADOW_DECISION_BYTES:
        raise CodecError("shadow decision exceeds the size limit")


def decode_shadow_row(row: Mapping[str, Any]) -> ShadowDecision:
    """Decode one versioned SQLite row into the shadow domain model."""

    try:
        if int(row["schema_version"]) != 1:
            raise CodecError("shadow decision schema is incompatible")
        decision = ShadowDecision(
            request_id=UUID(str(row["request_id"])),
            recorded_at=parse_utc(row["recorded_at"]),
            evaluated_at=parse_utc(row["evaluated_at"]),
            protocol=Protocol(row["protocol"]),
            requested_profile=str(row["requested_profile"]),
            actual_policy_hash=str(row["actual_policy_hash"]),
            candidate_policy_hash=str(row["candidate_policy_hash"]),
            candidate_algorithm_version=str(row["candidate_algorithm_version"]),
            actual_plan=decode_plan(str(row["actual_plan_json"])) if row["actual_plan_json"] else None,
            actual_error=(
                decode_error(str(row["actual_error_json"])) if row["actual_error_json"] else None
            ),
            candidate_plan=(
                decode_plan(str(row["candidate_plan_json"]))
                if row["candidate_plan_json"]
                else None
            ),
            candidate_error=(
                decode_error(str(row["candidate_error_json"]))
                if row["candidate_error_json"]
                else None
            ),
            status=ShadowStatus(row["status"]),
            change=ReplayChange(row["change"]) if row["change"] else None,
            reason=ShadowReason(row["reason"]) if row["reason"] else None,
        )
        if shadow_payload_hash(decision) != row["payload_hash"]:
            raise CodecError("shadow decision payload hash is invalid")
        return decision
    except (KeyError, TypeError, ValueError) as exc:
        raise CodecError("shadow decision is incompatible") from exc
