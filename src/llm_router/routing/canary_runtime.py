"""Startup-only assembly for shared Candidate, Shadow, and Canary routing."""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from llm_router.config import RouterConfig
from llm_router.evaluation.canary_models import CanaryReason
from llm_router.evaluation.canary_sqlite import SQLiteCanaryGateReader
from llm_router.evaluation.codec import make_policy_snapshot
from llm_router.evaluation.models import RoutingPolicySnapshot
from llm_router.evaluation.replay import ReplayEngine
from llm_router.evaluation.shadow import (
    NoopShadowEvaluator,
    ShadowEvaluator,
    ShadowEvaluatorPort,
    UnavailableShadowEvaluator,
)
from llm_router.evaluation.sqlite_store import SQLiteEvaluationStore
from llm_router.observability.metrics import RouterMetrics
from llm_router.routing.canary import (
    CanaryPolicySelector,
    CanaryRuntimeState,
    CurrentPolicySelector,
    PolicySelectorPort,
)
from llm_router.routing.candidate import CandidateBundle, CandidatePolicyLoader
from llm_router.routing.kernel import RoutingKernel


@dataclass(frozen=True, slots=True)
class CanaryComponents:
    """Return startup-fixed selector, Shadow evaluator, and snapshots."""

    selector: PolicySelectorPort
    shadow_port: ShadowEvaluatorPort
    shadow_evaluator: ShadowEvaluator | None
    current_snapshot: RoutingPolicySnapshot
    candidate_snapshot: RoutingPolicySnapshot | None
    state: CanaryRuntimeState


def _inactive_reason(
    config: RouterConfig,
    bundle: CandidateBundle | None,
    salt: bytes | None,
    gate_ok: bool,
) -> CanaryReason | None:
    """Choose one bounded startup reason in deterministic priority order."""

    if not config.replay.capture_enabled:
        return CanaryReason.CAPTURE_REQUIRED
    if bundle is None:
        return CanaryReason.CANDIDATE_UNAVAILABLE
    if bundle.expected_policy_hash != bundle.policy.routing_policy_hash:
        return CanaryReason.POLICY_HASH_MISMATCH
    if not bundle.catalog_compatible or not bundle.segments_compatible:
        return CanaryReason.CATALOG_INCOMPATIBLE
    if salt is None or len(salt) < 32:
        return CanaryReason.ASSIGNMENT_SALT_INVALID
    if not gate_ok:
        return CanaryReason.SHADOW_GATE_NOT_MET
    return None


def _gate_ok(
    config: RouterConfig,
    current_hash: str,
    bundle: CandidateBundle | None,
    now: datetime,
) -> bool:
    """Require each segment to meet its independent persisted Shadow gate."""

    if bundle is None:
        return False
    try:
        summaries = SQLiteCanaryGateReader(config.storage.sqlite_path).summaries(
            now - timedelta(seconds=config.canary.shadow_gate_lookback_seconds),
            now,
            current_hash,
            bundle.policy.routing_policy_hash,
            config.canary.segments,
        )
    except (sqlite3.Error, OSError):
        return False
    return all(
        summary.evaluated >= config.canary.minimum_shadow_evaluated
        and summary.non_replayable == 0
        and summary.evaluation_failed == 0
        for summary in summaries.values()
    ) and len(summaries) == len(config.canary.segments)


async def build_canary_components(
    config: RouterConfig,
    config_path: str,
    current_kernel: RoutingKernel,
    store: SQLiteEvaluationStore,
    metrics: RouterMetrics,
    now: datetime | None = None,
) -> CanaryComponents:
    """Load one Candidate, ensure snapshots, gate Canary, and build fixed adapters."""

    observed_at = (now or datetime.now(UTC)).astimezone(UTC)
    current_snapshot = make_policy_snapshot(config, observed_at)
    await store.ensure_policy(current_snapshot)
    bundle: CandidateBundle | None = None
    candidate_snapshot: RoutingPolicySnapshot | None = None
    candidate_needed = config.shadow.enabled or config.canary.enabled
    if candidate_needed:
        try:
            bundle = CandidatePolicyLoader(config, config_path).load(observed_at)
            candidate_snapshot = bundle.snapshot
            await store.ensure_policy(candidate_snapshot)
        except Exception:
            logging.getLogger("llm_router.canary").exception(
                "candidate policy is unavailable", extra={"event": "candidate_policy_unavailable"}
            )
    shadow_port: ShadowEvaluatorPort = NoopShadowEvaluator(metrics)
    shadow_evaluator: ShadowEvaluator | None = None
    if config.shadow.enabled:
        if bundle is None:
            shadow_port = UnavailableShadowEvaluator(metrics)
        else:
            shadow_evaluator = ShadowEvaluator(
                ReplayEngine(bundle.policy, "historical"),
                current_snapshot,
                store,
                config.shadow.sample_rate,
                frozenset(protocol.value for protocol in config.shadow.protocols),
                frozenset(config.shadow.profiles),
                config.shadow.queue_capacity,
                config.shadow.evaluation_timeout_ms,
                metrics=metrics,
            )
            shadow_port = shadow_evaluator
    if not config.canary.enabled:
        return CanaryComponents(
            CurrentPolicySelector(current_kernel),
            shadow_port,
            shadow_evaluator,
            current_snapshot,
            candidate_snapshot,
            CanaryRuntimeState(False, None),
        )
    salt_value = os.getenv(config.canary.assignment_salt_env)
    salt = salt_value.encode() if salt_value is not None else None
    gate_ok = _gate_ok(config, current_kernel.policy.routing_policy_hash, bundle, observed_at)
    reason = _inactive_reason(config, bundle, salt, gate_ok)
    state = CanaryRuntimeState(reason is None, reason)
    expected = config.candidate_policy.expected_policy_hash if config.candidate_policy else "0" * 64
    selector = CanaryPolicySelector(
        current_kernel,
        bundle,
        state,
        expected or "0" * 64,
        salt or b"",
        config.canary.threshold,
        frozenset((segment.protocol, segment.profile) for segment in config.canary.segments),
    )
    return CanaryComponents(
        selector, shadow_port, shadow_evaluator, current_snapshot, candidate_snapshot, state
    )
