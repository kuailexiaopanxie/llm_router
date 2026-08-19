"""FastAPI application assembly for Anthropic Messages and OpenAI Responses."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from llm_router.config import RouterConfig, load_candidate_config, load_config
from llm_router.domain import ModelTarget, Protocol
from llm_router.errors import not_ready
from llm_router.evaluation.codec import make_policy_snapshot
from llm_router.evaluation.models import RoutingPolicySnapshot
from llm_router.evaluation.outcomes import OutcomeService
from llm_router.evaluation.recorder import DecisionRecorder, NoopDecisionRecorder
from llm_router.evaluation.replay import ReplayEngine
from llm_router.evaluation.shadow import (
    NoopShadowEvaluator,
    ShadowEvaluator,
    ShadowEvaluatorPort,
    UnavailableShadowEvaluator,
)
from llm_router.evaluation.sqlite_store import SQLiteEvaluationStore
from llm_router.execution.engine import ExecutionEngine
from llm_router.execution.stream_semantics import (
    AnthropicStreamSemantics,
    OpenAIStreamSemantics,
)
from llm_router.gateway.anthropic import AnthropicGateway
from llm_router.gateway.openai import OpenAIResponsesGateway
from llm_router.gateway.outcomes import register_outcome_route
from llm_router.gateway.renderers import AnthropicErrorRenderer
from llm_router.health.coordinator import (
    DisabledHealthCoordinator,
    InMemoryHealthCoordinator,
)
from llm_router.health.models import AvailabilitySnapshot, HealthTransition
from llm_router.health.port import HealthPort
from llm_router.providers.registry import ProviderRegistry
from llm_router.routing.coordinator import RoutingCoordinator
from llm_router.routing.kernel import RoutingKernel
from llm_router.routing.policy import compile_routing_policy
from llm_router.routing.session import SessionStateStore
from llm_router.telemetry.metrics import RouterMetrics
from llm_router.telemetry.recorder import TelemetryRecorder
from llm_router.telemetry.sqlite_store import SQLiteEventStore


class JsonLogFormatter(logging.Formatter):
    """Render safe structured logs with English messages."""

    def format(self, record: logging.LogRecord) -> str:
        """Serialize standard and structured fields without request contents."""

        payload = {
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "event": getattr(record, "event", None),
            "request_id": getattr(record, "request_id", None),
            "stage": getattr(record, "stage", None),
            "provider": getattr(record, "provider", None),
            "target": getattr(record, "target", None),
            "target_alias": getattr(record, "target_alias", None),
            "from_state": getattr(record, "from_state", None),
            "to_state": getattr(record, "to_state", None),
            "failure_class": getattr(record, "failure_class", None),
        }
        if record.exc_info and record.exc_info[0] is not None:
            payload["exception_type"] = record.exc_info[0].__name__
        return json.dumps(payload, separators=(",", ":"))


def configure_logging() -> None:
    """Configure one process-wide JSON log handler."""

    root = logging.getLogger()
    if root.handlers:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonLogFormatter())
    root.addHandler(handler)
    root.setLevel(logging.INFO)


@dataclass
class Runtime:
    """Assembled application dependencies and shutdown state."""

    config: RouterConfig
    client_key: str
    providers: ProviderRegistry
    engine: ExecutionEngine
    kernel: RoutingKernel
    sessions: SessionStateStore
    telemetry: TelemetryRecorder
    metrics: RouterMetrics
    health: HealthPort
    coordinator: RoutingCoordinator
    evaluation: SQLiteEvaluationStore | None
    decision_recorder: DecisionRecorder | None
    shadow_evaluator: ShadowEvaluator | None
    candidate_policy_snapshot: RoutingPolicySnapshot | None
    ready: bool = False


def _shadow_candidate(
    config: RouterConfig,
    config_path: str,
    current_snapshot: RoutingPolicySnapshot,
    store: SQLiteEvaluationStore,
    metrics: RouterMetrics,
) -> tuple[ShadowEvaluatorPort, ShadowEvaluator | None, RoutingPolicySnapshot | None]:
    """Compile one fixed candidate without resolving its credentials."""

    if not config.shadow.enabled:
        return NoopShadowEvaluator(metrics), None, None
    logger = logging.getLogger("llm_router.evaluation.shadow")
    try:
        assert config.shadow.candidate_config_path is not None
        candidate_path = Path(config.shadow.candidate_config_path)
        if not candidate_path.is_absolute():
            candidate_path = Path(config_path).expanduser().parent / candidate_path
        candidate_config = load_candidate_config(candidate_path)
        candidate_policy = compile_routing_policy(candidate_config)
        candidate_snapshot = make_policy_snapshot(candidate_config, datetime.now(timezone.utc))
        if candidate_policy.routing_algorithm_version != current_snapshot.routing_algorithm_version:
            raise ValueError("candidate routing algorithm is incompatible")
        evaluator = ShadowEvaluator(
            ReplayEngine(candidate_policy, "historical"),
            current_snapshot,
            store,
            config.shadow.sample_rate,
            frozenset(protocol.value for protocol in config.shadow.protocols),
            frozenset(config.shadow.profiles),
            config.shadow.queue_capacity,
            config.shadow.evaluation_timeout_ms,
            metrics=metrics,
        )
        return evaluator, evaluator, candidate_snapshot
    except Exception:
        logger.exception("shadow candidate is unavailable", extra={"event": "shadow_candidate_invalid"})
        return UnavailableShadowEvaluator(metrics), None, None


def _health_observer(
    metrics: RouterMetrics,
    targets: dict[str, ModelTarget],
    snapshot: Callable[[], AvailabilitySnapshot],
) -> Callable[[HealthTransition], None]:
    """Build a best-effort transition observer outside coordinator locks."""

    provider_protocols = {target.provider: target.protocol.value for target in targets.values()}
    logger = logging.getLogger("llm_router.health")

    def observe(transition: HealthTransition) -> None:
        """Log and measure one bounded health transition."""

        logger.info(
            "provider health state changed",
            extra={
                "event": "provider_health_state_changed",
                "provider": transition.provider,
                "target": transition.target_alias,
                "target_alias": transition.target_alias,
                "from_state": transition.from_state.value,
                "to_state": transition.to_state.value,
                "failure_class": (
                    transition.failure_class.value if transition.failure_class is not None else None
                ),
            },
        )
        try:
            metrics.record_health_transition(
                transition,
                provider_protocols[transition.provider],
            )
            metrics.record_health_snapshot(snapshot(), targets)
        except Exception:
            metrics.health_update_failures.inc()
            logger.exception(
                "provider health metrics update failed",
                extra={"event": "provider_health_metrics_failed"},
            )

    return observe


def create_app(config_path: str = "router.yaml") -> FastAPI:
    """Create a configured FastAPI application from one validated YAML file."""

    configure_logging()
    config = load_config(config_path)
    client_key, provider_keys = config.resolve_secrets()
    providers = ProviderRegistry(config.providers, provider_keys)
    targets = config.model_targets()
    sessions = SessionStateStore(config.routing.session_ttl_seconds, config.routing.session_capacity)
    metrics = RouterMetrics()
    metrics.initialize_health(targets)
    health: HealthPort
    if config.health.enabled:
        health_reference: dict[str, HealthPort] = {}

        def health_snapshot() -> AvailabilitySnapshot:
            """Read the latest health view after an observed transition."""

            return health_reference["health"].snapshot(datetime.now(timezone.utc))

        health = InMemoryHealthCoordinator(
            config.health,
            targets,
            _health_observer(metrics, targets, health_snapshot),
        )
        health_reference["health"] = health
    else:
        health = DisabledHealthCoordinator(targets)
    telemetry = TelemetryRecorder(
        SQLiteEventStore(config.storage.sqlite_path), metrics, config.storage.queue_capacity
    )
    policy = compile_routing_policy(config)
    current_policy_snapshot = make_policy_snapshot(config, datetime.now(timezone.utc))
    evaluation = (
        SQLiteEvaluationStore(config.storage.sqlite_path)
        if config.outcomes.enabled or config.replay.capture_enabled or config.shadow.enabled
        else None
    )
    decision_recorder = (
        DecisionRecorder(evaluation, config.storage.queue_capacity, metrics)
        if config.replay.capture_enabled and evaluation is not None
        else None
    )
    recorder = decision_recorder or NoopDecisionRecorder()
    kernel = RoutingKernel(policy)
    shadow_port: ShadowEvaluatorPort = NoopShadowEvaluator(metrics)
    shadow_evaluator: ShadowEvaluator | None = None
    candidate_policy_snapshot: RoutingPolicySnapshot | None = None
    if evaluation is not None:
        shadow_port, shadow_evaluator, candidate_policy_snapshot = _shadow_candidate(
            config,
            config_path,
            current_policy_snapshot,
            evaluation,
            metrics,
        )
    coordinator = RoutingCoordinator(kernel, sessions, health, recorder, shadow_port)
    runtime = Runtime(
        config=config,
        client_key=client_key,
        providers=providers,
        engine=ExecutionEngine(
            {name: providers.get(name) for name in config.providers},
            {
                Protocol.ANTHROPIC_MESSAGES: AnthropicStreamSemantics(),
                Protocol.OPENAI_RESPONSES: OpenAIStreamSemantics(),
            },
            health,
            config.health.max_cooldown_seconds,
        ),
        kernel=kernel,
        sessions=sessions,
        telemetry=telemetry,
        metrics=metrics,
        health=health,
        coordinator=coordinator,
        evaluation=evaluation,
        decision_recorder=decision_recorder,
        shadow_evaluator=shadow_evaluator,
        candidate_policy_snapshot=candidate_policy_snapshot,
    )
    anthropic_gateway = AnthropicGateway(runtime)
    openai_gateway = OpenAIResponsesGateway(runtime)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """Start and stop telemetry/providers with a bounded shutdown drain."""

        await runtime.telemetry.start()
        if runtime.evaluation is not None:
            await runtime.evaluation.start()
            if runtime.decision_recorder is not None or runtime.shadow_evaluator is not None:
                await runtime.evaluation.ensure_policy(current_policy_snapshot)
            if runtime.candidate_policy_snapshot is not None:
                await runtime.evaluation.ensure_policy(runtime.candidate_policy_snapshot)
        if runtime.decision_recorder is not None and runtime.evaluation is not None:
            await runtime.decision_recorder.start()
        if runtime.shadow_evaluator is not None:
            await runtime.shadow_evaluator.start()
        runtime.ready = True
        try:
            yield
        finally:
            runtime.ready = False
            if runtime.shadow_evaluator is not None:
                await runtime.shadow_evaluator.close()
            if runtime.decision_recorder is not None:
                await runtime.decision_recorder.close()
            if runtime.evaluation is not None:
                await runtime.evaluation.close()
            await runtime.telemetry.close()
            await runtime.providers.close()

    app = FastAPI(title="Coding LLM Router", version="0.5.0", lifespan=lifespan)
    app.state.runtime = runtime
    if config.outcomes.enabled and evaluation is not None:
        register_outcome_route(
            app,
            OutcomeService(
                evaluation,
                config.outcomes.max_event_age_seconds,
                config.outcomes.max_future_skew_seconds,
            ),
            client_key,
            config.outcomes.max_request_bytes,
            metrics,
        )

    @app.post("/v1/messages")
    async def messages(request: Request) -> Response:
        """Proxy an Anthropic Messages request."""

        return await anthropic_gateway.handle(request)

    @app.post("/v1/messages/count_tokens")
    async def count_tokens(request: Request) -> Response:
        """Proxy a count-tokens request through the same routing kernel."""

        return await anthropic_gateway.handle(request, count_only=True)

    @app.post("/v1/responses")
    async def responses(request: Request) -> Response:
        """Proxy an OpenAI Responses request."""

        return await openai_gateway.handle(request)

    @app.get("/health")
    async def health_endpoint() -> dict[str, str]:
        """Return process liveness without checking dependencies."""

        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> Response:
        """Return readiness only after telemetry initialization completes."""

        if not runtime.ready:
            error = not_ready()
            return AnthropicErrorRenderer().json_error(error, "readiness")
        return JSONResponse({"status": "ready"})

    @app.get("/metrics")
    async def metrics_endpoint() -> Response:
        """Expose local Prometheus metrics."""

        return Response(content=runtime.metrics.render(), media_type="text/plain; version=0.5.0")

    return app


def main() -> None:
    """Run the local Uvicorn server from command-line configuration."""

    parser = argparse.ArgumentParser(description="Run the Coding LLM Router")
    parser.add_argument("--config", default="router.yaml", help="Path to router YAML")
    args = parser.parse_args()
    import uvicorn

    config = load_config(args.config)
    uvicorn.run(create_app(args.config), host=config.server.host, port=config.server.port)


if __name__ == "__main__":
    main()
