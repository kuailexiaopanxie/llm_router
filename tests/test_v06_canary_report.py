"""Read-only actual Canary report acceptance tests."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

from conftest import router_config_data

from llm_router.cli import run_canary_report
from llm_router.config import RouterConfig
from llm_router.evaluation.canary_models import (
    AffinityKind,
    CanaryAssignment,
    CanaryReason,
    PolicyRole,
)
from llm_router.evaluation.codec import make_policy_snapshot
from llm_router.evaluation.models import RouteDecisionInput
from llm_router.evaluation.sqlite_store import SQLiteEvaluationStore
from llm_router.health.coordinator import DisabledHealthCoordinator
from llm_router.routing.context import RoutingContext
from llm_router.routing.features import extract_routing_request
from llm_router.routing.kernel import RoutingKernel
from llm_router.routing.policy import compile_routing_policy
from llm_router.telemetry.sqlite_store import SQLiteEventStore


def test_canary_report_is_read_only_private_and_denominator_explicit(tmp_path, capsys) -> None:
    """Report captured assignment gaps without exposing affinity or causal claims."""

    async def prepare() -> None:
        """Create compatible telemetry and evaluation tables with one assignment."""

        path = str(tmp_path / "router.db")
        telemetry = SQLiteEventStore(path)
        await telemetry.start()
        await telemetry.close()
        config = RouterConfig.model_validate(router_config_data())
        policy = compile_routing_policy(config)
        now = datetime.now(UTC)
        request = extract_routing_request({"messages": []}, "code/auto")
        availability = DisabledHealthCoordinator(config.model_targets()).snapshot(now)
        plan = RoutingKernel(policy).plan(request, RoutingContext(None, availability))
        store = SQLiteEvaluationStore(path)
        await store.start()
        await store.ensure_policy(make_policy_snapshot(config, now))
        await store.append_decision(
            RouteDecisionInput(
                uuid4(),
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
                    PolicyRole.CONTROL,
                    CanaryReason.CONTROL_BUCKET,
                    policy.routing_policy_hash,
                    policy.routing_policy_hash,
                    AffinityKind.SESSION,
                    9000,
                    100,
                ),
            )
        )
        await store.close()

    asyncio.run(prepare())
    database = tmp_path / "router.db"
    before = database.read_bytes()
    assert run_canary_report(["--db", str(database), "--format", "json"]) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    assert database.read_bytes() == before
    assert report["roles"]["control"]["assigned"] == 1
    assert report["roles"]["control"]["completion_gaps"] == 1
    assert report["roles"]["control"]["outcome_coverage"]["denominator"] == 1
    assert "session-" not in output
    assert all(term not in output.lower() for term in ("uplift", "significance", "promote"))
