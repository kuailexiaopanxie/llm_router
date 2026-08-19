"""OpenAI Responses protocol gateway."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from llm_router.domain import Protocol, ProtocolEnvelope
from llm_router.errors import RouterError, internal_error, invalid_request
from llm_router.gateway.auth import (
    authenticate,
    request_id,
    safe_headers,
    session_id,
    task_id,
)
from llm_router.gateway.common import (
    finish_error,
    provider_extension_headers,
    read_json_object,
    record_completion,
    route_headers,
    routing_facts,
)
from llm_router.gateway.renderers import OpenAIErrorRenderer
from llm_router.observability.models import EndpointKind, RequestStatus, TerminalStage
from llm_router.observability.tracing import trace_context
from llm_router.routing.coordinator import RoutingInvocation
from llm_router.routing.openai_features import extract_routing_request


class OpenAIResponsesGateway:
    """Route OpenAI Responses requests without protocol translation."""

    def __init__(self, runtime: Any) -> None:
        """Bind assembled router dependencies and the OpenAI renderer."""

        self._runtime = runtime
        self._errors = OpenAIErrorRenderer()

    async def handle(self, request: Request) -> Response:
        """Handle one OpenAI Responses request."""

        runtime = self._runtime
        config = runtime.config
        rid = request_id(request.headers)
        received_at = datetime.now(UTC)
        trace = trace_context(
            request.headers.get("traceparent"),
            config.observability.tracing.accept_traceparent,
        )
        lifecycle = runtime.observation_registry.open(
            UUID(rid),
            trace,
            received_at,
            EndpointKind.RESPONSES,
        )
        stage = TerminalStage.AUTHENTICATION
        envelope = None
        routing_request = None
        try:
            authenticate(request.headers, runtime.client_key)
            stage = TerminalStage.VALIDATION
            body = await read_json_object(request, config.server.max_request_bytes)
            model = body.get("model", config.routing.default_profile)
            if not isinstance(model, str) or not model:
                raise invalid_request("The model field must be a configured string.")
            protocol = Protocol.OPENAI_RESPONSES
            task = task_id(request.headers)
            session_key = session_id(request.headers)
            envelope = ProtocolEnvelope(
                request_id=rid,
                protocol=protocol,
                raw_body=body,
                safe_headers=safe_headers(request.headers, provider_extension_headers(config, protocol)),
                stream=body.get("stream") is True,
                received_at=received_at,
                endpoint="/v1/responses",
                traceparent=f"00-{trace.trace_id}-{trace.root_span_id}-01",
            )
            routing_request = extract_routing_request(body, model)
            observed_profile = model if model in config.profiles else None
            lifecycle.request_facts(
                protocol,
                observed_profile,
                envelope.stream,
                UUID(task) if task else None,
            )
            stage = TerminalStage.ROUTING
            if runtime.coordinator is None:
                raise internal_error()
            resolution = runtime.coordinator.resolve(
                RoutingInvocation(
                    UUID(rid),
                    UUID(task) if task else None,
                    session_key,
                    envelope.received_at,
                    routing_request,
                )
            )
            lifecycle.routed(routing_facts(resolution, observed_profile or "unknown"))
            if resolution.error is not None:
                raise resolution.error
            assert resolution.plan is not None
            plan = resolution.plan
            stage = TerminalStage.EXECUTION_PRE_COMMIT
            response = await runtime.engine.execute(envelope, plan)
            lifecycle.execution_started(envelope.stream)
            headers = route_headers(envelope, plan, response, trace.trace_id)
            asyncio.create_task(
                record_completion(
                    runtime,
                    envelope,
                    routing_request,
                    response,
                    lifecycle,
                    session_key,
                )
            )
            headers.update(response.headers)
            if isinstance(response.body, bytes):
                return Response(
                    content=response.body,
                    status_code=response.status_code,
                    headers=headers,
                    media_type=response.media_type,
                )
            return StreamingResponse(
                response.body,
                status_code=response.status_code,
                headers=headers,
                media_type=response.media_type,
            )
        except RouterError as error:
            finish_error(lifecycle, error, stage)
            return self._errors.json_error(error, rid, trace.trace_id)
        except asyncio.CancelledError:
            lifecycle.finish(RequestStatus.CANCELLED, stage, "router_cancelled")
            raise
        except Exception:  # noqa: BLE001 - sanitize the endpoint boundary.
            logging.getLogger("llm_router.gateway").error(
                "Request handling failed",
                extra={
                    "event": "request_handling_failed",
                    "request_id": rid,
                    "stage": stage.value,
                },
            )
            response_error = internal_error()
            finish_error(lifecycle, response_error, stage)
            return self._errors.json_error(response_error, rid, trace.trace_id)
