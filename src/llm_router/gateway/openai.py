"""OpenAI Responses protocol gateway."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
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
    provider_extension_headers,
    read_json_object,
    record_completion,
    record_route_failure,
    route_headers,
)
from llm_router.gateway.renderers import OpenAIErrorRenderer
from llm_router.routing.coordinator import RoutingInvocation
from llm_router.routing.openai_features import extract_routing_request


class OpenAIResponsesGateway:
    """Route OpenAI Responses requests without protocol translation."""

    def __init__(self, runtime: Any) -> None:
        """Bind assembled router dependencies and the OpenAI renderer."""

        self._runtime = runtime
        self._errors = OpenAIErrorRenderer()

    @staticmethod
    def _usage(payload: bytes) -> tuple[int | None, int | None]:
        """Extract aggregate token counts from a non-stream response when present."""

        try:
            body = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None, None
        usage = body.get("usage") if isinstance(body, dict) else None
        if not isinstance(usage, dict):
            return None, None
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        return (
            input_tokens if isinstance(input_tokens, int) else None,
            output_tokens if isinstance(output_tokens, int) else None,
        )

    async def handle(self, request: Request) -> Response:
        """Handle one OpenAI Responses request."""

        runtime = self._runtime
        config = runtime.config
        rid = request_id(request.headers)
        stage = "authenticate"
        envelope = None
        routing_request = None
        availability = None
        plan = None
        task = None
        selected_policy_version = None
        try:
            authenticate(request.headers, runtime.client_key)
            stage = "read_request"
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
                received_at=datetime.now(timezone.utc),
                endpoint="/v1/responses",
            )
            stage = "route"
            routing_request = extract_routing_request(body, model)
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
            selected_policy_version = resolution.policy_version
            if resolution.error is not None:
                raise resolution.error
            assert resolution.plan is not None
            plan = resolution.plan
            stage = "execute"
            response = await runtime.engine.execute(envelope, plan)
            stage = "build_response"
            headers = route_headers(envelope, plan, response)
            asyncio.create_task(
                record_completion(
                    runtime, envelope, routing_request, plan, response, self._usage, task, session_key
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
            if (
                error.code == "router_no_available_target"
                and envelope is not None
                and routing_request is not None
            ):
                record_route_failure(
                    runtime,
                    envelope,
                    routing_request,
                    availability,
                    error,
                    plan,
                    task,
                    selected_policy_version,
                )
            return self._errors.json_error(error, rid)
        except Exception:
            logging.getLogger("llm_router.gateway").exception(
                "request handling failed",
                extra={
                    "event": "request_handling_failed",
                    "request_id": rid,
                    "stage": stage,
                },
            )
            response_error = internal_error() if stage == "route" else invalid_request(
                "The request could not be processed."
            )
            return self._errors.json_error(response_error, rid)
