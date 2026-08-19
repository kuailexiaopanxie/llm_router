"""Canonical codec and SQLite helpers for Canary assignment metadata."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from llm_router.evaluation.canary_models import (
    AffinityKind,
    CanaryAssignment,
    CanaryReason,
    PolicyRole,
)
from llm_router.evaluation.codec import CodecError, canonical_json


def encode_canary_assignment(assignment: CanaryAssignment | None) -> str | None:
    """Encode bounded assignment fields without raw affinity or HMAC material."""

    if assignment is None:
        return None
    return canonical_json(
        {
            "role": assignment.role.value,
            "reason": assignment.reason.value,
            "expected_candidate_policy_hash": assignment.expected_candidate_policy_hash,
            "candidate_policy_hash": assignment.candidate_policy_hash,
            "affinity_kind": assignment.affinity_kind.value,
            "bucket": assignment.bucket,
            "threshold": assignment.threshold,
        }
    )


def decode_canary_assignment(payload: str | None) -> CanaryAssignment | None:
    """Decode one strictly versioned optional assignment payload."""

    if payload is None:
        return None
    try:
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise CodecError("canary assignment is incompatible")
        expected = {
            "role",
            "reason",
            "expected_candidate_policy_hash",
            "candidate_policy_hash",
            "affinity_kind",
            "bucket",
            "threshold",
        }
        if set(value) != expected:
            raise CodecError("canary assignment schema is incompatible")
        return CanaryAssignment(
            role=PolicyRole(value["role"]),
            reason=CanaryReason(value["reason"]),
            expected_candidate_policy_hash=str(value["expected_candidate_policy_hash"]),
            candidate_policy_hash=(
                str(value["candidate_policy_hash"])
                if value["candidate_policy_hash"] is not None
                else None
            ),
            affinity_kind=AffinityKind(value["affinity_kind"]),
            bucket=int(value["bucket"]) if value["bucket"] is not None else None,
            threshold=int(value["threshold"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CodecError("canary assignment is incompatible") from exc


def assignment_from_decision_row(row: Mapping[str, Any]) -> CanaryAssignment | None:
    """Decode v1 as null and v2 from its additive SQLite column."""

    schema_version = int(row["schema_version"])
    if schema_version == 1:
        return None
    if schema_version == 2:
        raw = row.get("canary_assignment_json")
        return decode_canary_assignment(str(raw)) if raw is not None else None
    raise CodecError("decision schema is incompatible")
