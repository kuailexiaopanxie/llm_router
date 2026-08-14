"""FastAPI application assembly and Anthropic-compatible HTTP gateway."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from llm_router.config import RouterConfig, load_config
from llm_router.execution.engine import ExecutionEngine
from llm_router.gateway.anthropic import AnthropicGateway
from llm_router.gateway.errors import not_ready
from llm_router.providers.anthropic import ProviderRegistry
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
    ready: bool = False


def create_app(config_path: str = "router.yaml") -> FastAPI:
    """Create a configured FastAPI application from one validated YAML file."""

    configure_logging()
    config = load_config(config_path)
    client_key, provider_keys = config.resolve_secrets()
    providers = ProviderRegistry(config.providers, provider_keys)
    sessions = SessionStateStore(config.routing.session_ttl_seconds, config.routing.session_capacity)
    metrics = RouterMetrics()
    telemetry = TelemetryRecorder(
        SQLiteEventStore(config.storage.sqlite_path), metrics, config.storage.queue_capacity
    )
    runtime = Runtime(
        config=config,
        client_key=client_key,
        providers=providers,
        engine=ExecutionEngine({name: providers.get(name) for name in config.providers}),
        kernel=RoutingKernel(config, sessions),
        sessions=sessions,
        telemetry=telemetry,
        metrics=metrics,
    )
    gateway = AnthropicGateway(runtime)

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

    app = FastAPI(title="Coding LLM Router", version="0.1.0", lifespan=lifespan)
    app.state.runtime = runtime

    @app.post("/v1/messages")
    async def messages(request: Request) -> Response:
        """Proxy an Anthropic Messages request."""

        return await gateway.handle(request)

    @app.post("/v1/messages/count_tokens")
    async def count_tokens(request: Request) -> Response:
        """Proxy a count-tokens request through the same routing kernel."""

        return await gateway.handle(request, count_only=True)

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Return process liveness without checking dependencies."""

        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> Response:
        """Return readiness only after telemetry initialization completes."""

        if not runtime.ready:
            error = not_ready()
            return JSONResponse(
                status_code=error.http_status,
                content={
                    "type": "error",
                    "error": {"type": error.anthropic_type, "message": error.message},
                },
            )
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
