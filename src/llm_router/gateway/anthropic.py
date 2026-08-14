"""Anthropic Messages protocol gateway."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from llm_router.domain import ExecutionStats, OutcomeSignal, ProtocolEnvelope, RouteEvent
from llm_router.gateway.auth import authenticate, request_id, safe_headers
from llm_router.gateway.errors import RouterError, invalid_request
from llm_router.routing.features import extract_routing_request, summarize_features


class AnthropicGateway:
    """Translate HTTP requests into routing plans and protocol-transparent responses."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    @staticmethod
    def _error_response(error: RouterError, request_id_value: str) -> JSONResponse:
        """Render a normalized Anthropic-compatible router error."""

        return JSONResponse(
            status_code=error.http_status,
            headers={"x-llm-router-request-id": request_id_value},
            content={
                "type": "error",
                "error": {"type": error.anthropic_type, "message": error.message},
                "request_id": request_id_value,
            },
        )

    @staticmethod
    async def _read_json(request: Request, max_bytes: int) -> dict[str, Any]:
        """Read and validate one bounded JSON object without retaining raw bytes."""

        content_length = request.headers.get("content-length")
        if content_length and (not content_length.isdigit() or int(content_length) > max_bytes):
            raise invalid_request("The request body exceeds the configured size limit.")
        body = await request.body()
        if len(body) > max_bytes:
            raise invalid_request("The request body exceeds the configured size limit.")
        try:
            value = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise invalid_request("The request body must be valid JSON.") from exc
        if not isinstance(value, dict):
            raise invalid_request("The request body must be a JSON object.")
        if isinstance(value.get("base_url"), str) or isinstance(value.get("provider"), str):
            raise invalid_request("The request cannot select an upstream URL or provider.")
        return value

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

    @staticmethod
    def _route_headers(envelope: ProtocolEnvelope, plan: Any, response: Any) -> dict[str, str]:
        """Build stable router response headers from the resolved execution plan."""

        route_reason = plan.route_reason
        if plan.auxiliary_reasons:
            route_reason = ",".join((route_reason, *plan.auxiliary_reasons))
        return {
            "x-llm-router-request-id": envelope.request_id,
            "x-llm-router-profile": plan.profile,
            "x-llm-router-upstream-model": response.final_target.upstream_model,
            "x-llm-router-route-reason": route_reason[:256],
            "x-llm-router-policy-version": plan.policy_version,
            "x-llm-router-attempts": str(response.attempt_count),
        }

    async def _record_completion(
        self, envelope: ProtocolEnvelope, routing_request: Any, plan: Any, response: Any
    ) -> None:
        """Record completed execution and update opt-in session state."""

        runtime = self._runtime
        try:
            stats: ExecutionStats = await response.completion
            if isinstance(response.body, bytes):
                input_tokens, output_tokens = self._usage(response.body)
                stats = replace(stats, input_tokens=input_tokens, output_tokens=output_tokens)
            status = stats.status
            outcome = routing_request.outcome_signal
            if outcome is OutcomeSignal.UNKNOWN:
                outcome = OutcomeSignal.SUCCESS if status == "success" else OutcomeSignal.FAILURE
            if not envelope.endpoint.endswith("count_tokens"):
                runtime.sessions.record(routing_request.session_id, response.final_target.tier, outcome)
            runtime.telemetry.record(
                RouteEvent(
                    request_id=envelope.request_id,
                    received_at=envelope.received_at,
                    protocol=envelope.protocol,
                    profile=plan.profile,
                    stream=envelope.stream,
                    feature_summary=summarize_features(routing_request),
                    primary_model=plan.primary.alias,
                    final_model=response.final_target.alias,
                    route_reason=plan.route_reason,
                    policy_version=plan.policy_version,
                    status=status,
                    attempt_count=response.attempt_count,
                    time_to_first_event_ms=stats.time_to_first_event_ms,
                    total_latency_ms=stats.total_latency_ms,
                    input_tokens=stats.input_tokens,
                    output_tokens=stats.output_tokens,
                    error_code=stats.error_code,
                    attempts=response.attempts,
                )
            )
        except Exception:
            logging.getLogger("llm_router.gateway").exception(
                "completion telemetry failed",
                extra={"event": "completion_telemetry_failed", "request_id": envelope.request_id},
            )

    async def handle(self, request: Request, count_only: bool = False) -> Response:
        """Handle one Anthropic Messages or count-tokens request."""

        runtime = self._runtime
        config = runtime.config
        rid = request_id(request.headers)
        stage = "authenticate"
        try:
            authenticate(request.headers, runtime.client_key)
            stage = "read_request"
            body = await self._read_json(request, config.server.max_request_bytes)
            model = body.get("model", config.routing.default_profile)
            if not isinstance(model, str) or not model:
                raise invalid_request("The model field must be a configured string.")
            session_id = request.headers.get("x-llm-router-session-id")
            extension_headers = tuple(
                header for provider_config in config.providers.values() for header in provider_config.extension_headers
            )
            envelope = ProtocolEnvelope(
                request_id=rid,
                protocol="anthropic_messages",
                raw_body=body,
                safe_headers=safe_headers(request.headers, extension_headers),
                stream=body.get("stream") is True and not count_only,
                received_at=datetime.now(timezone.utc),
                endpoint="/v1/messages/count_tokens" if count_only else "/v1/messages",
            )
            stage = "route"
            routing_request = extract_routing_request(body, model, session_id, count_only=count_only)
            plan = runtime.kernel.plan(routing_request)
            stage = "execute"
            response = await runtime.engine.execute(envelope, plan)
            stage = "build_response"
            route_headers = self._route_headers(envelope, plan, response)
            asyncio.create_task(self._record_completion(envelope, routing_request, plan, response))
            route_headers.update(response.headers)
            if isinstance(response.body, bytes):
                return Response(
                    content=response.body,
                    status_code=response.status_code,
                    headers=route_headers,
                    media_type=response.media_type,
                )
            return StreamingResponse(
                response.body,
                status_code=response.status_code,
                headers=route_headers,
                media_type=response.media_type,
            )
        except RouterError as error:
            return self._error_response(error, rid)
        except Exception:
            logging.getLogger("llm_router.gateway").exception(
                "request handling failed",
                extra={
                    "event": "request_handling_failed",
                    "request_id": rid,
                    "stage": stage,
                },
            )
            return self._error_response(invalid_request("The request could not be processed."), rid)
