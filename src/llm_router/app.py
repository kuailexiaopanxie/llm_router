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

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from llm_router.config import RouterConfig, load_config
from llm_router.domain import ModelTarget, Protocol
from llm_router.errors import RouterError, not_ready
from llm_router.evaluation.outcomes import OutcomeService
from llm_router.evaluation.recorder import DecisionRecorder, NoopDecisionRecorder
from llm_router.evaluation.shadow import ShadowEvaluator
from llm_router.evaluation.sqlite_store import SQLiteEvaluationStore
from llm_router.execution.engine import ExecutionEngine
from llm_router.execution.stream_semantics import (
    AnthropicStreamSemantics,
    OpenAIStreamSemantics,
)
from llm_router.gateway.anthropic import AnthropicGateway
from llm_router.gateway.auth import authenticate, request_id
from llm_router.gateway.openai import OpenAIResponsesGateway
from llm_router.gateway.outcomes import register_outcome_route
from llm_router.gateway.renderers import AnthropicErrorRenderer
from llm_router.health.coordinator import (
    DisabledHealthCoordinator,
    InMemoryHealthCoordinator,
)
from llm_router.health.models import AvailabilitySnapshot, HealthTransition
from llm_router.health.port import HealthPort
from llm_router.observability.hub import ObservationHub
from llm_router.observability.lifecycle import ActiveObservationRegistry
from llm_router.observability.metrics import RouterMetrics
from llm_router.observability.otlp import OtlpTraceExporter
from llm_router.observability.port import NoopTraceExporter
from llm_router.observability.pricing import CostCalculator, PricingCatalog
from llm_router.observability.retention import RetentionWorker
from llm_router.observability.sqlite_store import SQLiteObservationStore
from llm_router.observability.tracing import TraceBuilder
from llm_router.providers.registry import ProviderRegistry
from llm_router.routing.canary import (
    CanaryRuntimeState,
    CurrentPolicySelector,
    PolicySelectorPort,
)
from llm_router.routing.canary_runtime import build_canary_components
from llm_router.routing.coordinator import RoutingCoordinator
from llm_router.routing.kernel import RoutingKernel
from llm_router.routing.policy import compile_routing_policy
from llm_router.routing.session import SessionStateStore


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
    observations: ObservationHub
    observation_store: SQLiteObservationStore
    observation_registry: ActiveObservationRegistry
    retention: RetentionWorker | None
    metrics: RouterMetrics
    health: HealthPort
    coordinator: RoutingCoordinator | None
    evaluation: SQLiteEvaluationStore | None
    decision_recorder: DecisionRecorder | None
    shadow_evaluator: ShadowEvaluator | None
    canary_state: CanaryRuntimeState
    ready: bool = False


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
        except Exception:  # noqa: BLE001 - health observation remains fail-open.
            metrics.health_update_failures.inc()
            logger.error(
                "Provider health metrics update failed",
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
    observation_store = SQLiteObservationStore(config.storage.sqlite_path)
    exporter = (
        OtlpTraceExporter(config.observability.tracing.otlp, metrics)
        if config.observability.tracing.otlp.enabled
        else NoopTraceExporter()
    )
    observations = ObservationHub(
        observation_store,
        metrics,
        CostCalculator(PricingCatalog.from_config(config)),
        TraceBuilder(
            config.observability.tracing.sample_rate,
            config.observability.tracing.enabled,
        ),
        exporter,
        config.observability.capture_enabled,
        config.observability.queue_capacity,
        config.observability.tracing.local_store,
    )
    retention = (
        RetentionWorker(
            config.storage.sqlite_path,
            config.observability.retention_days,
            metrics,
        )
        if config.observability.capture_enabled
        and config.observability.retention_days is not None
        else None
    )
    policy = compile_routing_policy(config)
    evaluation = (
        SQLiteEvaluationStore(config.storage.sqlite_path)
        if (
            config.outcomes.enabled
            or config.replay.capture_enabled
            or config.shadow.enabled
            or config.canary.enabled
        )
        else None
    )
    decision_recorder = (
        DecisionRecorder(evaluation, config.storage.queue_capacity, metrics)
        if config.replay.capture_enabled and evaluation is not None
        else None
    )
    recorder = decision_recorder or NoopDecisionRecorder()
    kernel = RoutingKernel(policy)
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
        observations=observations,
        observation_store=observation_store,
        observation_registry=ActiveObservationRegistry(observations, metrics),
        retention=retention,
        metrics=metrics,
        health=health,
        coordinator=None,
        evaluation=evaluation,
        decision_recorder=decision_recorder,
        shadow_evaluator=None,
        canary_state=CanaryRuntimeState(False, None),
    )
    anthropic_gateway = AnthropicGateway(runtime)
    openai_gateway = OpenAIResponsesGateway(runtime)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """Start and stop observations and providers with bounded drains."""

        if runtime.config.observability.capture_enabled:
            await runtime.observation_store.start()
        await runtime.observations.start()
        if runtime.retention is not None:
            await runtime.retention.start()
        selector: PolicySelectorPort = CurrentPolicySelector(runtime.kernel)
        shadow_port = None
        if runtime.evaluation is not None:
            await runtime.evaluation.start()
            components = await build_canary_components(
                runtime.config,
                config_path,
                runtime.kernel,
                runtime.evaluation,
                runtime.metrics,
            )
            selector = components.selector
            shadow_port = components.shadow_port
            runtime.shadow_evaluator = components.shadow_evaluator
            runtime.canary_state = components.state
        if runtime.decision_recorder is not None and runtime.evaluation is not None:
            await runtime.decision_recorder.start()
        if runtime.shadow_evaluator is not None:
            await runtime.shadow_evaluator.start()
        runtime.coordinator = RoutingCoordinator(
            selector,
            runtime.sessions,
            runtime.health,
            recorder,
            shadow_port,
            runtime.metrics,
        )
        runtime_state = (
            "disabled"
            if not runtime.config.canary.enabled
            else "active"
            if runtime.canary_state.active
            else "inactive"
        )
        runtime.metrics.record_canary_runtime(
            runtime_state,
            runtime.canary_state.reason.value if runtime.canary_state.reason else None,
        )
        runtime.ready = True
        try:
            yield
        finally:
            runtime.ready = False
            runtime.coordinator = None
            runtime.observation_registry.abandon_all()
            if runtime.shadow_evaluator is not None:
                await runtime.shadow_evaluator.close()
            if runtime.decision_recorder is not None:
                await runtime.decision_recorder.close()
            if runtime.evaluation is not None:
                await runtime.evaluation.close()
            if runtime.retention is not None:
                await runtime.retention.close()
            await runtime.observations.close()
            await runtime.observation_store.close()
            await runtime.providers.close()

    app = FastAPI(title="Coding LLM Router", version="0.7.0", lifespan=lifespan)
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
        """Return readiness only after observation initialization completes."""

        if not runtime.ready:
            error = not_ready()
            return AnthropicErrorRenderer().json_error(error, "readiness")
        return JSONResponse({"status": "ready"})

    @app.get("/metrics")
    async def metrics_endpoint(request: Request) -> Response:
        """Expose local Prometheus metrics."""

        if not runtime.config.observability.metrics.enabled:
            return Response(status_code=404)
        if runtime.config.observability.metrics.require_auth:
            try:
                authenticate(request.headers, runtime.client_key)
            except RouterError as error:
                return AnthropicErrorRenderer().json_error(error, request_id(request.headers))
        return Response(content=runtime.metrics.render(), media_type="text/plain; version=0.7.0")

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
