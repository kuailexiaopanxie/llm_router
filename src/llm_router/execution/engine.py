"""Execution engine enforcing bounded attempts and SSE commit semantics."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Mapping
from datetime import datetime, timezone

from llm_router.domain import (
    AttemptEvent,
    ExecutionPlan,
    ExecutionStats,
    Protocol,
    ProtocolEnvelope,
    ProviderExchange,
    ProviderRequest,
    ProxyResponse,
)
from llm_router.errors import (
    RouterError,
    cancelled_error,
    no_available_target,
    response_header_timeout,
    timeout_error,
    upstream_exhausted,
    upstream_rejected,
)
from llm_router.execution.failures import (
    FailureDecision,
    decision_for_http,
    decision_for_router_error,
    router_error_for_failure,
)
from llm_router.execution.stream_semantics import StreamSemantics
from llm_router.execution.streaming import read_first_event, relay_stream
from llm_router.health.models import (
    AttemptOutcome,
    AvailabilitySnapshot,
    FailureClass,
    HealthLease,
)
from llm_router.health.port import HealthPort
from llm_router.providers.port import ProviderFailure, ProviderPort


class ExecutionEngine:
    """Execute only the targets contained in an immutable execution plan."""

    def __init__(
        self,
        providers: Mapping[str, ProviderPort],
        stream_semantics: Mapping[Protocol, StreamSemantics],
        health: HealthPort,
        max_retry_after_seconds: float,
    ) -> None:
        """Bind provider ports, stream semantics, and health admission."""

        self._providers = providers
        self._stream_semantics = stream_semantics
        self._health = health
        self._max_retry_after_seconds = max_retry_after_seconds

    @staticmethod
    def _content_type(headers: Mapping[str, str]) -> str:
        """Read a normalized content type from upstream response headers."""

        return headers.get("content-type", "application/json").split(";", 1)[0].strip().lower()

    @staticmethod
    def _header(headers: Mapping[str, str], name: str) -> str | None:
        """Read one upstream header without relying on mapping case behavior."""

        normalized = name.lower()
        for key, value in headers.items():
            if key.lower() == normalized:
                return value
        return None

    async def _json_body(
        self, exchange: ProviderExchange, deadline: float, header_timeout: float
    ) -> bytes:
        """Collect a non-stream response before committing bytes downstream."""

        chunks: list[bytes] = []
        iterator = exchange.body.__aiter__()
        first_deadline = min(deadline, time.monotonic() + header_timeout)
        first_chunk = True
        while True:
            remaining = (first_deadline if first_chunk else deadline) - time.monotonic()
            if remaining <= 0:
                raise timeout_error()
            try:
                chunks.append(await asyncio.wait_for(iterator.__anext__(), remaining))
                first_chunk = False
            except StopAsyncIteration:
                return b"".join(chunks)
            except asyncio.TimeoutError as exc:
                raise response_header_timeout() if first_chunk else timeout_error() from exc
            except Exception as exc:
                raise RouterError(
                    "router_upstream_read_failed",
                    503,
                    "The upstream response could not be read.",
                    fallback_allowed=True,
                ) from exc

    @staticmethod
    def _retry_after(snapshot: AvailabilitySnapshot) -> float | None:
        """Calculate bounded whole recovery seconds from a fresh snapshot."""

        if snapshot.earliest_recovery_at is None:
            return None
        remaining = (snapshot.earliest_recovery_at - snapshot.observed_at).total_seconds()
        return float(max(0, math.ceil(remaining)))

    async def execute(self, envelope: ProtocolEnvelope, plan: ExecutionPlan) -> ProxyResponse:
        """Execute a routing plan while preserving pre-commit fallback behavior."""

        started = time.monotonic()
        deadline_budget = (
            plan.timeouts.stream_max_seconds if envelope.stream else plan.timeouts.non_stream_deadline_seconds
        )
        deadline = started + deadline_budget
        attempts: list[AttemptEvent] = []
        last_error: RouterError | None = None
        upstream_attempt_count = 0
        health_skipped_count = 0
        event_sequence = 0
        for target_index, target in enumerate(plan.targets, start=1):
            if time.monotonic() >= deadline:
                last_error = timeout_error()
                break
            attempt_started = time.monotonic()
            attempt_wall = datetime.now(timezone.utc)
            if target.protocol is not envelope.protocol:
                raise RouterError(
                    "router_protocol_mismatch",
                    500,
                    "The execution plan contains an incompatible protocol target.",
                )
            lease = self._health.acquire(target, attempt_wall)
            if lease is None:
                event_sequence += 1
                health_skipped_count += 1
                attempts.append(
                    AttemptEvent(
                        request_id=envelope.request_id,
                        sequence=event_sequence,
                        provider=target.provider,
                        model=target.alias,
                        started_at=attempt_wall,
                        duration_ms=(time.monotonic() - attempt_started) * 1000,
                        status="health_skipped",
                    )
                )
                continue
            exchange: ProviderExchange | None = None
            lease_recorded = False
            failure_decision: FailureDecision | None = None
            upstream_http_status: int | None = None
            admitted_lease = lease
            assert admitted_lease is not None

            def record_lease(
                outcome: AttemptOutcome,
                bound_lease: HealthLease | None = admitted_lease,
            ) -> None:
                """Apply one admitted attempt outcome at most once."""

                nonlocal lease_recorded
                assert bound_lease is not None
                if lease_recorded:
                    return
                lease_recorded = True
                self._health.record(bound_lease, outcome)

            try:
                provider = self._providers[target.provider]
                response_header_budget = min(
                    plan.timeouts.response_header_seconds,
                    max(0.1, deadline - time.monotonic()),
                )
                try:
                    upstream_attempt_count += 1
                    exchange = await asyncio.wait_for(
                        provider.invoke(
                            ProviderRequest(
                                envelope=envelope,
                                target=target,
                                connect_timeout=min(
                                    plan.timeouts.connect_seconds,
                                    max(0.1, deadline - time.monotonic()),
                                ),
                                response_header_timeout=response_header_budget,
                            )
                        ),
                        timeout=response_header_budget,
                    )
                except asyncio.TimeoutError as exc:
                    raise response_header_timeout() from exc
                upstream_http_status = exchange.status_code
                if exchange.status_code < 200 or exchange.status_code >= 300:
                    failure_decision = decision_for_http(
                        exchange.status_code,
                        self._header(exchange.headers, "retry-after"),
                        datetime.now(timezone.utc),
                        self._max_retry_after_seconds,
                    )
                    await exchange.close()
                    exchange = None
                    record_lease(
                        AttemptOutcome(
                            failure_decision.failure_class,
                            datetime.now(timezone.utc),
                            failure_decision.retry_after_seconds,
                        )
                    )
                    raise failure_decision.router_error
                if envelope.stream:
                    if self._content_type(exchange.headers) != "text/event-stream":
                        raise upstream_rejected()
                    try:
                        first, remainder = await asyncio.wait_for(
                            read_first_event(exchange),
                            max(0.1, min(plan.timeouts.response_header_seconds, deadline - time.monotonic())),
                        )
                    except asyncio.TimeoutError as exc:
                        raise response_header_timeout() from exc
                    semantics = self._stream_semantics[envelope.protocol]
                    semantics.validate_first_event(first)
                    completion: asyncio.Future[ExecutionStats] = asyncio.get_running_loop().create_future()
                    body = relay_stream(
                        exchange,
                        first,
                        remainder,
                        deadline,
                        plan.timeouts.stream_idle_seconds,
                        completion,
                        started,
                        semantics,
                        record_lease,
                    )
                    event_sequence += 1
                    attempts.append(
                        AttemptEvent(
                            request_id=envelope.request_id,
                            sequence=event_sequence,
                            provider=target.provider,
                            model=target.alias,
                            started_at=attempt_wall,
                            duration_ms=(time.monotonic() - attempt_started) * 1000,
                            status="committed",
                            http_status=exchange.status_code,
                        )
                    )
                    return ProxyResponse(
                        status_code=exchange.status_code,
                        headers={
                            "content-type": "text/event-stream",
                            "cache-control": "no-cache",
                            **(
                                {"request-id": exchange.headers["request-id"]}
                                if "request-id" in exchange.headers
                                else {}
                            ),
                        },
                        body=body,
                        media_type="text/event-stream",
                        final_target=target,
                        attempt_count=upstream_attempt_count,
                        completion=completion,
                        attempts=tuple(attempts),
                        health_skipped_count=health_skipped_count,
                    )
                content_type = self._content_type(exchange.headers)
                upstream_status = exchange.status_code
                upstream_request_id = exchange.headers.get("request-id")
                payload = await self._json_body(
                    exchange,
                    deadline,
                    min(plan.timeouts.response_header_seconds, max(0.1, deadline - time.monotonic())),
                )
                await exchange.close()
                exchange = None
                record_lease(AttemptOutcome(FailureClass.SUCCESS, datetime.now(timezone.utc)))
                completion = asyncio.get_running_loop().create_future()
                completion.set_result(
                    ExecutionStats(status="success", total_latency_ms=(time.monotonic() - started) * 1000)
                )
                event_sequence += 1
                attempts.append(
                    AttemptEvent(
                        request_id=envelope.request_id,
                        sequence=event_sequence,
                        provider=target.provider,
                        model=target.alias,
                        started_at=attempt_wall,
                        duration_ms=(time.monotonic() - attempt_started) * 1000,
                        status="success",
                        http_status=upstream_status,
                    )
                )
                return ProxyResponse(
                    status_code=upstream_status,
                    headers={
                        "content-type": content_type,
                        **({"request-id": upstream_request_id} if upstream_request_id else {}),
                    },
                    body=payload,
                    media_type="application/json",
                    final_target=target,
                    attempt_count=upstream_attempt_count,
                    completion=completion,
                    attempts=tuple(attempts),
                    health_skipped_count=health_skipped_count,
                )
            except asyncio.CancelledError:
                if exchange is not None:
                    await exchange.close()
                record_lease(
                    AttemptOutcome(FailureClass.CLIENT_CANCELLED, datetime.now(timezone.utc))
                )
                raise cancelled_error()
            except ProviderFailure as exc:
                failure_decision = FailureDecision(
                    exc.failure_class,
                    router_error_for_failure(exc.failure_class),
                    exc.retry_after_seconds,
                )
                failure_decision.router_error.retry_after = exc.retry_after_seconds
                last_error = failure_decision.router_error
                record_lease(
                    AttemptOutcome(
                        failure_decision.failure_class,
                        datetime.now(timezone.utc),
                        failure_decision.retry_after_seconds,
                    )
                )
            except RouterError as exc:
                if exchange is not None:
                    await exchange.close()
                last_error = exc
                if failure_decision is None:
                    failure_decision = decision_for_router_error(exc)
                record_lease(
                    AttemptOutcome(
                        failure_decision.failure_class,
                        datetime.now(timezone.utc),
                        failure_decision.retry_after_seconds,
                    )
                )
            # Provider adapters form an isolation boundary for unknown transport errors.
            except Exception:  # noqa: BLE001
                if exchange is not None:
                    await exchange.close()
                failure_class = FailureClass.PROVIDER_TRANSIENT
                last_error = router_error_for_failure(failure_class)
                record_lease(AttemptOutcome(failure_class, datetime.now(timezone.utc)))
            event_sequence += 1
            assert last_error is not None
            attempts.append(
                AttemptEvent(
                    request_id=envelope.request_id,
                    sequence=event_sequence,
                    provider=target.provider,
                    model=target.alias,
                    started_at=attempt_wall,
                    duration_ms=(time.monotonic() - attempt_started) * 1000,
                    status="failed",
                    error_code=last_error.code,
                    http_status=upstream_http_status,
                )
            )
            if not last_error.fallback_allowed or target_index >= len(plan.targets):
                break
        if upstream_attempt_count == 0 and health_skipped_count:
            snapshot = self._health.snapshot(datetime.now(timezone.utc))
            error = no_available_target(self._retry_after(snapshot))
            error.health_snapshot_revision = snapshot.revision
            error.health_filtered_count = plan.health_filtered_count
            error.health_skipped_count = health_skipped_count
            error.health_reason = "health_lease_unavailable"
            error.health_skipped_attempts = tuple(attempts)
            raise error
        if last_error is not None and last_error.code == "router_timeout":
            raise last_error
        if last_error is not None and not last_error.fallback_allowed:
            raise last_error
        raise upstream_exhausted()
