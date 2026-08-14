"""Execution engine enforcing bounded attempts and SSE commit semantics."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Mapping
from datetime import datetime, timezone

from llm_router.domain import (
    AttemptEvent,
    ExecutionPlan,
    ExecutionStats,
    ProtocolEnvelope,
    ProviderExchange,
    ProviderRequest,
    ProxyResponse,
)
from llm_router.gateway.errors import (
    RouterError,
    cancelled_error,
    response_header_timeout,
    timeout_error,
    upstream_exhausted,
    upstream_rejected,
)
from llm_router.providers.port import ProviderPort


_RETRYABLE_STATUS = {429, 500, 502, 503, 504, 529}
_ERROR_EVENT = b'event: error\ndata: {"type":"error","error":{"type":"api_error","message":"The upstream stream failed."}}\n\n'


class ExecutionEngine:
    """Execute only the targets contained in an immutable execution plan."""

    def __init__(self, providers: Mapping[str, ProviderPort]) -> None:
        self._providers = providers

    @staticmethod
    def _content_type(headers: Mapping[str, str]) -> str:
        """Read a normalized content type from upstream response headers."""

        return headers.get("content-type", "application/json").split(";", 1)[0].strip().lower()

    @staticmethod
    async def _read_first_event(exchange: ProviderExchange) -> tuple[bytes, bytes]:
        """Read one complete SSE event and preserve all bytes after its delimiter."""

        buffer = bytearray()
        try:
            async for chunk in exchange.body:
                buffer.extend(chunk)
                for delimiter in (b"\n\n", b"\r\n\r\n"):
                    index = buffer.find(delimiter)
                    if index >= 0:
                        end = index + len(delimiter)
                        first = bytes(buffer[:end])
                        remainder = bytes(buffer[end:])
                        if first.strip():
                            return first, remainder
        except RouterError:
            raise
        except Exception as exc:
            raise RouterError(
                "router_upstream_read_failed",
                503,
                "The upstream response could not be read.",
                "overloaded_error",
                fallback_allowed=True,
            ) from exc
        raise RouterError(
            "router_upstream_empty_stream",
            503,
            "The upstream stream ended before its first event.",
            "overloaded_error",
            fallback_allowed=True,
        )

    async def _json_body(
        self, exchange: ProviderExchange, deadline: float, response_header_timeout: float
    ) -> bytes:
        """Collect a non-stream response before committing bytes downstream."""

        chunks: list[bytes] = []
        iterator = exchange.body.__aiter__()
        first_deadline = min(deadline, time.monotonic() + response_header_timeout)
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
                    "overloaded_error",
                    fallback_allowed=True,
                ) from exc

    async def _stream_body(
        self,
        exchange: ProviderExchange,
        first_event: bytes,
        remainder: bytes,
        deadline: float,
        idle_timeout: float,
        completion: asyncio.Future[ExecutionStats],
        started: float,
    ) -> AsyncIterator[bytes]:
        """Yield a committed SSE stream and convert post-commit errors to an error event."""

        first_event_at = time.monotonic()
        try:
            yield first_event
            if remainder:
                yield remainder
            iterator = exchange.body.__aiter__()
            while True:
                remaining = min(deadline - time.monotonic(), idle_timeout)
                if remaining <= 0:
                    raise timeout_error()
                try:
                    chunk = await asyncio.wait_for(iterator.__anext__(), remaining)
                except StopAsyncIteration:
                    completion.set_result(
                        ExecutionStats(
                            status="success",
                            total_latency_ms=(time.monotonic() - started) * 1000,
                            time_to_first_event_ms=(first_event_at - started) * 1000,
                        )
                    )
                    return
                yield chunk
        except asyncio.CancelledError:
            if not completion.done():
                completion.set_result(
                    ExecutionStats(status="cancelled", total_latency_ms=(time.monotonic() - started) * 1000, error_code="router_cancelled")
                )
            raise
        except RouterError as exc:
            if not completion.done():
                completion.set_result(
                    ExecutionStats(
                        status="error",
                        total_latency_ms=(time.monotonic() - started) * 1000,
                        error_code=exc.code,
                    )
                )
            yield _ERROR_EVENT
        except Exception:
            if not completion.done():
                completion.set_result(
                    ExecutionStats(
                        status="error",
                        total_latency_ms=(time.monotonic() - started) * 1000,
                        error_code="router_upstream_read_failed",
                    )
                )
            yield _ERROR_EVENT
        finally:
            await exchange.close()

    async def execute(self, envelope: ProtocolEnvelope, plan: ExecutionPlan) -> ProxyResponse:
        """Execute a routing plan while preserving pre-commit fallback behavior."""

        started = time.monotonic()
        deadline_budget = (
            plan.timeouts.stream_max_seconds if envelope.stream else plan.timeouts.non_stream_deadline_seconds
        )
        deadline = started + deadline_budget
        attempts: list[AttemptEvent] = []
        last_error: RouterError | None = None
        for sequence, target in enumerate(plan.targets, start=1):
            if time.monotonic() >= deadline:
                last_error = timeout_error()
                break
            attempt_started = time.monotonic()
            attempt_wall = datetime.now(timezone.utc)
            exchange: ProviderExchange | None = None
            try:
                provider = self._providers[target.provider]
                exchange = await provider.invoke(
                    ProviderRequest(
                        envelope=envelope,
                        target=target,
                        connect_timeout=min(plan.timeouts.connect_seconds, max(0.1, deadline - time.monotonic())),
                        response_header_timeout=min(
                            plan.timeouts.response_header_seconds, max(0.1, deadline - time.monotonic())
                        ),
                    )
                )
                if exchange.status_code < 200 or exchange.status_code >= 300:
                    retryable = exchange.status_code in _RETRYABLE_STATUS
                    await exchange.close()
                    exchange = None
                    raise RouterError(
                        "router_upstream_retryable" if retryable else "router_upstream_rejected",
                        503 if retryable else 502,
                        "The upstream request failed." if retryable else "The upstream rejected the request.",
                        "overloaded_error" if retryable else "api_error",
                        fallback_allowed=retryable,
                    )
                if envelope.stream:
                    if self._content_type(exchange.headers) != "text/event-stream":
                        raise upstream_rejected()
                    try:
                        first, remainder = await asyncio.wait_for(
                            self._read_first_event(exchange),
                            max(0.1, min(plan.timeouts.response_header_seconds, deadline - time.monotonic())),
                        )
                    except asyncio.TimeoutError as exc:
                        raise response_header_timeout() from exc
                    completion: asyncio.Future[ExecutionStats] = asyncio.get_running_loop().create_future()
                    body = self._stream_body(
                        exchange,
                        first,
                        remainder,
                        deadline,
                        plan.timeouts.stream_idle_seconds,
                        completion,
                        started,
                    )
                    attempts.append(
                        AttemptEvent(
                            request_id=envelope.request_id,
                            sequence=sequence,
                            provider=target.provider,
                            model=target.alias,
                            started_at=attempt_wall,
                            duration_ms=(time.monotonic() - attempt_started) * 1000,
                            status="committed",
                            http_status=exchange.status_code,
                        )
                    )
                    return ProxyResponse(
                        status_code=200,
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
                        attempt_count=sequence,
                        completion=completion,
                        attempts=tuple(attempts),
                    )
                content_type = self._content_type(exchange.headers)
                upstream_request_id = exchange.headers.get("request-id")
                payload = await self._json_body(
                    exchange,
                    deadline,
                    min(plan.timeouts.response_header_seconds, max(0.1, deadline - time.monotonic())),
                )
                await exchange.close()
                exchange = None
                completion = asyncio.get_running_loop().create_future()
                completion.set_result(
                    ExecutionStats(status="success", total_latency_ms=(time.monotonic() - started) * 1000)
                )
                attempts.append(
                    AttemptEvent(
                        request_id=envelope.request_id,
                        sequence=sequence,
                        provider=target.provider,
                        model=target.alias,
                        started_at=attempt_wall,
                        duration_ms=(time.monotonic() - attempt_started) * 1000,
                        status="success",
                        http_status=200,
                    )
                )
                return ProxyResponse(
                    status_code=200,
                    headers={
                        "content-type": content_type,
                        **({"request-id": upstream_request_id} if upstream_request_id else {}),
                    },
                    body=payload,
                    media_type="application/json",
                    final_target=target,
                    attempt_count=sequence,
                    completion=completion,
                    attempts=tuple(attempts),
                )
            except asyncio.CancelledError:
                if exchange is not None:
                    await exchange.close()
                raise cancelled_error()
            except (RouterError, asyncio.TimeoutError) as exc:
                if exchange is not None:
                    await exchange.close()
                last_error = timeout_error() if isinstance(exc, asyncio.TimeoutError) else exc
                attempts.append(
                    AttemptEvent(
                        request_id=envelope.request_id,
                        sequence=sequence,
                        provider=target.provider,
                        model=target.alias,
                        started_at=attempt_wall,
                        duration_ms=(time.monotonic() - attempt_started) * 1000,
                        status="failed",
                        error_code=last_error.code,
                        http_status=last_error.http_status,
                    )
                )
                if not last_error.fallback_allowed or sequence >= len(plan.targets):
                    break
        if last_error is not None and last_error.code == "router_timeout":
            raise last_error
        if last_error is not None and not last_error.fallback_allowed:
            raise last_error
        raise upstream_exhausted()
