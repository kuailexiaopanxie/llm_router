"""FastAPI application assembly for Anthropic Messages and OpenAI Responses."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from llm_router.config import RouterConfig, load_config
from llm_router.domain import ModelTarget, Protocol
from llm_router.execution.engine import ExecutionEngine
from llm_router.execution.stream_semantics import AnthropicStreamSemantics, OpenAIStreamSemantics
from llm_router.gateway.anthropic import AnthropicGateway
from llm_router.gateway.errors import not_ready
from llm_router.gateway.openai import OpenAIResponsesGateway
from llm_router.gateway.renderers import AnthropicErrorRenderer
from llm_router.health.coordinator import DisabledHealthCoordinator, InMemoryHealthCoordinator
from llm_router.health.models import AvailabilitySnapshot, HealthTransition
from llm_router.health.port import HealthPort
from llm_router.providers.registry import ProviderRegistry
from llm_router.routing.kernel import RoutingKernel
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
        if record.exc_info:
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
        kernel=RoutingKernel(config, sessions),
        sessions=sessions,
        telemetry=telemetry,
        metrics=metrics,
        health=health,
    )
    anthropic_gateway = AnthropicGateway(runtime)
    openai_gateway = OpenAIResponsesGateway(runtime)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        """Start and stop telemetry/providers with a bounded shutdown drain."""

        await runtime.telemetry.start()
        runtime.ready = True
        try:
            yield
        finally:
            runtime.ready = False
            await runtime.telemetry.close()
            await runtime.providers.close()

    app = FastAPI(title="Coding LLM Router", version="0.3.0", lifespan=lifespan)
    app.state.runtime = runtime

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
    async def health() -> dict[str, str]:
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
    async def metrics() -> Response:
        """Expose local Prometheus metrics."""

        return Response(content=runtime.metrics.render(), media_type="text/plain; version=0.0.4")

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
