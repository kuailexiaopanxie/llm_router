"""Bounded persisted domain values for controlled Canary assignment."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class PolicyRole(StrEnum):
    """Identify which policy produced the actual result."""

    CONTROL = "control"
    CANARY = "canary"


class AffinityKind(StrEnum):
    """Identify the opaque input category used for stable assignment."""

    NONE = "none"
    SESSION = "session"
    TASK = "task"
    REQUEST = "request"


class CanaryReason(StrEnum):
    """Bound all Canary selection and inactive-state reasons."""

    CANARY_BUCKET = "canary_bucket"
    CONTROL_BUCKET = "control_bucket"
    SEGMENT_FILTERED = "segment_filtered"
    AFFINITY_REQUIRED = "affinity_required"
    COUNT_ONLY_EXCLUDED = "count_only_excluded"
    CANDIDATE_UNAVAILABLE = "candidate_unavailable"
    POLICY_HASH_MISMATCH = "policy_hash_mismatch"
    CATALOG_INCOMPATIBLE = "catalog_incompatible"
    SHADOW_GATE_NOT_MET = "shadow_gate_not_met"
    CAPTURE_REQUIRED = "capture_required"
    ASSIGNMENT_SALT_INVALID = "assignment_salt_invalid"
    SELECTOR_FAILURE = "selector_failure"


@dataclass(frozen=True, slots=True)
class CanaryAssignment:
    """Store selection metadata without raw affinity, digest, or salt."""

    role: PolicyRole
    reason: CanaryReason
    expected_candidate_policy_hash: str
    candidate_policy_hash: str | None
    affinity_kind: AffinityKind
    bucket: int | None
    threshold: int

    def __post_init__(self) -> None:
        """Validate bounded hash and bucket combinations."""

        hash_pattern = r"[0-9a-f]{64}"
        if re.fullmatch(hash_pattern, self.expected_candidate_policy_hash) is None:
            raise ValueError("expected candidate policy hash is invalid")
        if (
            self.candidate_policy_hash is not None
            and re.fullmatch(hash_pattern, self.candidate_policy_hash) is None
        ):
            raise ValueError("candidate policy hash is invalid")
        if not 0 <= self.threshold <= 2500:
            raise ValueError("canary threshold is invalid")
        if self.bucket is not None and not 0 <= self.bucket <= 9999:
            raise ValueError("canary bucket is invalid")
        bucket_reason = self.reason in {CanaryReason.CANARY_BUCKET, CanaryReason.CONTROL_BUCKET}
        if bucket_reason != (self.bucket is not None):
            raise ValueError("bucket assignments require exactly one bounded bucket")
        if bucket_reason and self.affinity_kind is AffinityKind.NONE:
            raise ValueError("bucket assignments require one bounded affinity kind")
        if not bucket_reason and self.affinity_kind is not AffinityKind.NONE:
            raise ValueError("ineligible assignments cannot retain affinity metadata")
        if self.reason is CanaryReason.CANARY_BUCKET and self.role is not PolicyRole.CANARY:
            raise ValueError("canary_bucket reason requires canary role")
        if self.reason is CanaryReason.CONTROL_BUCKET and self.role is not PolicyRole.CONTROL:
            raise ValueError("control_bucket reason requires control role")
        if self.role is PolicyRole.CANARY and (
            self.reason is not CanaryReason.CANARY_BUCKET
            or self.candidate_policy_hash is None
        ):
            raise ValueError("canary role requires canary_bucket reason")
