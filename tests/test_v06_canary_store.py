"""Canary assignment persistence, migration, and replay compatibility tests."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from conftest import router_config_data

from llm_router.canary_config import CanarySegmentConfig
from llm_router.config import RouterConfig
from llm_router.evaluation.canary_models import (
    AffinityKind,
    CanaryAssignment,
    CanaryReason,
    PolicyRole,
)
from llm_router.evaluation.canary_sqlite import SQLiteCanaryGateReader
from llm_router.evaluation.codec import make_policy_snapshot
from llm_router.evaluation.models import (
    ReplayChange,
    RouteDecisionInput,
    ShadowDecision,
    ShadowStatus,
)
from llm_router.evaluation.replay import ReplayEngine
from llm_router.evaluation.sqlite_store import SQLiteEvaluationStore, SQLiteReplayStore
from llm_router.health.coordinator import DisabledHealthCoordinator
from llm_router.routing.context import RoutingContext
from llm_router.routing.features import extract_routing_request
from llm_router.routing.kernel import RoutingKernel
from llm_router.routing.policy import compile_routing_policy


def test_canary_migration_is_idempotent_and_v1_v2_decode(tmp_path) -> None:
    """Add the assignment column once and preserve legacy replay semantics."""

    async def prepare() -> tuple[str, str]:
        """Persist one legacy and one Canary-assigned decision."""

        path = str(tmp_path / "router.db")
        config = RouterConfig.model_validate(router_config_data())
        policy = compile_routing_policy(config)
        kernel = RoutingKernel(policy)
        now = datetime.now(UTC)
        request = extract_routing_request({"messages": []}, "code/auto")
        availability = DisabledHealthCoordinator(config.model_targets()).snapshot(now)
        plan = kernel.plan(request, RoutingContext(None, availability))
        store = SQLiteEvaluationStore(path)
        await store.start()
        await store.ensure_policy(make_policy_snapshot(config, now))
        legacy_id = uuid4()
        canary_id = uuid4()
        await store.append_decision(
            RouteDecisionInput(
                legacy_id,
                None,
                now,
                "0.5.0",
                policy.routing_algorithm_version,
                policy.routing_policy_hash,
                request,
                None,
                availability,
                plan,
                schema_version=1,
            )
        )
        await store.append_decision(
            RouteDecisionInput(
                canary_id,
                None,
                now,
                "0.6.0",
                policy.routing_algorithm_version,
                policy.routing_policy_hash,
                request,
                None,
                availability,
                plan,
                canary_assignment=CanaryAssignment(
                    PolicyRole.CANARY,
                    CanaryReason.CANARY_BUCKET,
                    policy.routing_policy_hash,
                    policy.routing_policy_hash,
                    AffinityKind.REQUEST,
                    1,
                    100,
                ),
            )
        )
        await store.close()
        await store.start()
        await store.close()
        return str(legacy_id), str(canary_id)

    legacy_id, canary_id = asyncio.run(prepare())
    with sqlite3.connect(tmp_path / "router.db") as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(route_decision_inputs)")]
    assert columns.count("canary_assignment_json") == 1
    cases = {
        str(case.decision.request_id): case
        for case in SQLiteReplayStore(str(tmp_path / "router.db")).iter_cases(None, None, 10)
    }
    assert cases[legacy_id].decision.canary_assignment is None
    assert cases[canary_id].decision.canary_assignment is not None
    policy = compile_routing_policy(RouterConfig.model_validate(router_config_data()))
    assert ReplayEngine(policy, "historical").replay(cases[legacy_id]).status.value == "replayed"
    assert ReplayEngine(policy, "historical").replay(cases[canary_id]).status.value == "replayed"


def test_shadow_gate_counts_each_declared_segment_independently(tmp_path) -> None:
    """Prevent one high-volume segment from satisfying another segment's gate."""

    async def prepare() -> tuple[datetime, str, str]:
        """Persist one evaluated Anthropic sample and no OpenAI sample."""

        config = RouterConfig.model_validate(router_config_data())
        policy = compile_routing_policy(config)
        now = datetime.now(UTC)
        request = extract_routing_request({"messages": []}, "code/auto")
        availability = DisabledHealthCoordinator(config.model_targets()).snapshot(now)
        plan = RoutingKernel(policy).plan(request, RoutingContext(None, availability))
        request_id = uuid4()
        candidate_hash = policy.routing_policy_hash
        store = SQLiteEvaluationStore(str(tmp_path / "router.db"))
        await store.start()
        await store.ensure_policy(make_policy_snapshot(config, now))
        await store.append_decision(
            RouteDecisionInput(
                request_id,
                None,
                now,
                "0.6.0",
                policy.routing_algorithm_version,
                policy.routing_policy_hash,
                request,
                None,
                availability,
                plan,
            )
        )
        await store.append_shadow(
            ShadowDecision(
                request_id,
                now,
                now,
                request.protocol,
                "code/auto",
                policy.routing_policy_hash,
                candidate_hash,
                policy.routing_algorithm_version,
                plan,
                None,
                plan,
                None,
                ShadowStatus.EVALUATED,
                ReplayChange.UNCHANGED,
            )
        )
        await store.close()
        return now, policy.routing_policy_hash, candidate_hash

    now, current_hash, candidate_hash = asyncio.run(prepare())
    summaries = SQLiteCanaryGateReader(str(tmp_path / "router.db")).summaries(
        now,
        now + timedelta(seconds=1),
        current_hash,
        candidate_hash,
        (
            CanarySegmentConfig(protocol="anthropic_messages", profile="code/auto"),
            CanarySegmentConfig(protocol="openai_responses", profile="code/auto"),
        ),
    )
    assert summaries[("anthropic_messages", "code/auto")].evaluated == 1
    assert summaries[("openai_responses", "code/auto")].evaluated == 0
