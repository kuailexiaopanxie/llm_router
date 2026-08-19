"""HTTP adapter for authenticated bounded Outcome Event submission."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, ValidationError

from llm_router.errors import RouterError
from llm_router.evaluation.models import (
    OutcomeEvent,
    OutcomeEvidence,
    OutcomeSource,
    OutcomeVerdict,
)
from llm_router.evaluation.outcomes import (
    OutcomeService,
    OutcomeUnavailableError,
    OutcomeValidationError,
)
from llm_router.evaluation.port import OutcomeConflictError
from llm_router.gateway.auth import authenticate, request_id
from llm_router.observability.metrics import RouterMetrics


class OutcomeRequest(BaseModel):
    """Strict client-controlled fields accepted by the Outcome endpoint."""

    model_config = ConfigDict(extra="forbid", strict=True)

    event_id: UUID
    request_id: UUID
    task_id: UUID | None = None
    verdict: OutcomeVerdict
    evidence: OutcomeEvidence
    source: OutcomeSource
    observed_at: datetime | None = None


def _error(status: int, code: str, message: str, rid: str) -> JSONResponse:
    """Render a protocol-neutral router-control error response."""

    return JSONResponse(
        {"error": {"code": code, "message": message}, "request_id": rid},
        status_code=status,
        headers={"x-llm-router-request-id": rid},
    )


def register_outcome_route(
    app: FastAPI,
    service: OutcomeService,
    client_key: str,
    max_request_bytes: int,
    metrics: RouterMetrics | None = None,
) -> None:
    """Register the Outcome endpoint only when explicitly enabled."""

    @app.post("/v1/router/outcomes")
    async def submit_outcome(request: Request) -> Response:
        """Authenticate, validate, and synchronously persist one event."""

        rid = request_id(request.headers)
        try:
            authenticate(request.headers, client_key)
        except RouterError as exc:
            if metrics is not None:
                metrics.outcome_rejected.labels("unauthorized").inc()
            return _error(exc.http_status, exc.code, exc.safe_message, rid)
        content_length = request.headers.get("content-length")
        if content_length and (
            not content_length.isdigit() or int(content_length) > max_request_bytes
        ):
            if metrics is not None:
                metrics.outcome_rejected.labels("body_too_large").inc()
            return _error(413, "outcome_invalid", "The request body is too large.", rid)
        body = await request.body()
        if len(body) > max_request_bytes:
            if metrics is not None:
                metrics.outcome_rejected.labels("body_too_large").inc()
            return _error(413, "outcome_invalid", "The request body is too large.", rid)
        try:
            payload = OutcomeRequest.model_validate_json(body, strict=True)
            if payload.observed_at is not None and payload.observed_at.tzinfo is None:
                raise ValueError("observed_at must include a timezone")
            receipt = await service.submit(
                OutcomeEvent(
                    event_id=payload.event_id,
                    request_id=payload.request_id,
                    task_id=payload.task_id,
                    verdict=payload.verdict,
                    evidence=payload.evidence,
                    source=payload.source,
                    observed_at=payload.observed_at,
                    received_at=datetime.now(UTC),
                )
            )
        except (ValidationError, ValueError, OutcomeValidationError):
            if metrics is not None:
                metrics.outcome_rejected.labels("invalid").inc()
            return _error(422, "outcome_invalid", "The Outcome Event is invalid.", rid)
        except OutcomeConflictError:
            if metrics is not None:
                metrics.outcome_rejected.labels("conflict").inc()
            return _error(409, "outcome_event_conflict", "The event ID conflicts with stored data.", rid)
        except OutcomeUnavailableError:
            if metrics is not None:
                metrics.outcome_rejected.labels("store_unavailable").inc()
            return _error(503, "outcome_store_unavailable", "The Outcome store is unavailable.", rid)
        if metrics is not None:
            metrics.outcomes.labels(
                payload.verdict.value,
                payload.evidence.value,
                payload.source.value,
                receipt.status,
                receipt.correlation.value,
            ).inc()
        return JSONResponse(
            {
                "event_id": str(receipt.event_id),
                "status": receipt.status,
                "correlation": receipt.correlation.value,
            },
            status_code=201 if receipt.status == "accepted" else 200,
            headers={"x-llm-router-request-id": rid},
        )
