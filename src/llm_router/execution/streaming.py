"""Protocol-neutral SSE reading, observation, and downstream relay."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone

from llm_router.domain import ExecutionStats, ProviderExchange
from llm_router.errors import RouterError, timeout_error
from llm_router.execution.stream_semantics import StreamSemantics
from llm_router.health.models import AttemptOutcome, FailureClass
from llm_router.observability.models import UsageBreakdown, UsageStatus
from llm_router.observability.usage import merge_usage

_MAX_USAGE_EVENT_BYTES = 4 * 1024 * 1024


class SSEUsageAccumulator:
    """Observe complete SSE events without changing proxied stream bytes."""

    def __init__(self, semantics: StreamSemantics) -> None:
        """Initialize a bounded observer for one protocol stream."""

        self._semantics = semantics
        self._buffer = bytearray()
        self.usage = UsageBreakdown.missing()
        self.overflowed = False
        self.terminal_outcome: FailureClass | None = None

    @staticmethod
    def delimiter(buffer: bytearray) -> tuple[int, int] | None:
        """Locate the earliest supported SSE event delimiter."""

        candidates = [
            (index, len(delimiter))
            for delimiter in (b"\n\n", b"\r\n\r\n")
            if (index := buffer.find(delimiter)) >= 0
        ]
        return min(candidates) if candidates else None

    def feed(self, chunk: bytes) -> None:
        """Consume raw stream bytes and retain only bounded parser state."""

        self._buffer.extend(chunk)
        while delimiter := self.delimiter(self._buffer):
            index, length = delimiter
            end = index + length
            event = bytes(self._buffer[:end])
            del self._buffer[:end]
            fragment = self._semantics.extract_usage(event)
            terminal_outcome = self._semantics.terminal_outcome(event)
            if fragment is not None:
                self.usage = merge_usage(self.usage, fragment)
            if terminal_outcome is not None:
                self.terminal_outcome = terminal_outcome
        if self.overflowed and self.usage.status is UsageStatus.COMPLETE:
            self.usage = UsageBreakdown(
                UsageStatus.PARTIAL,
                self.usage.input_uncached_tokens,
                self.usage.input_cache_read_tokens,
                self.usage.input_cache_write_tokens,
                self.usage.output_tokens,
                self.usage.reasoning_output_tokens,
            )
        if len(self._buffer) > _MAX_USAGE_EVENT_BYTES:
            self._buffer.clear()
            self.overflowed = True
            if self.usage.status is not UsageStatus.INVALID:
                self.usage = UsageBreakdown(
                    UsageStatus.PARTIAL,
                    self.usage.input_uncached_tokens,
                    self.usage.input_cache_read_tokens,
                    self.usage.input_cache_write_tokens,
                    self.usage.output_tokens,
                    self.usage.reasoning_output_tokens,
                )


async def read_first_event(exchange: ProviderExchange) -> tuple[bytes, bytes]:
    """Read one complete SSE event and preserve bytes after its delimiter."""

    buffer = bytearray()
    try:
        async for chunk in exchange.body:
            buffer.extend(chunk)
            found = SSEUsageAccumulator.delimiter(buffer)
            if found is not None:
                index, length = found
                end = index + length
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
            fallback_allowed=True,
        ) from exc
    raise RouterError(
        "router_upstream_empty_stream",
        503,
        "The upstream stream ended before its first event.",
        fallback_allowed=True,
    )


async def relay_stream(
    exchange: ProviderExchange,
    first_event: bytes,
    remainder: bytes,
    deadline: float,
    idle_timeout: float,
    completion: asyncio.Future[ExecutionStats],
    started: float,
    semantics: StreamSemantics,
    record_outcome: Callable[[AttemptOutcome], None],
) -> AsyncIterator[bytes]:
    """Relay a committed SSE stream and render protocol-native terminal errors."""

    first_event_at = time.monotonic()
    usage = SSEUsageAccumulator(semantics)
    usage.feed(first_event)
    if remainder:
        usage.feed(remainder)
    outcome_recorded = False

    def finish(failure_class: FailureClass) -> None:
        """Record the admitted stream lease exactly once."""

        nonlocal outcome_recorded
        if outcome_recorded:
            return
        outcome_recorded = True
        record_outcome(AttemptOutcome(failure_class, datetime.now(timezone.utc)))

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
                if usage.terminal_outcome is FailureClass.POST_COMMIT_STREAM_FAILURE:
                    finish(FailureClass.POST_COMMIT_STREAM_FAILURE)
                    completion.set_result(
                        ExecutionStats(
                            status="error",
                            total_latency_ms=(time.monotonic() - started) * 1000,
                            time_to_first_event_ms=(first_event_at - started) * 1000,
                            usage=usage.usage,
                            error_code="router_upstream_stream_error",
                        )
                    )
                    return
                if usage.terminal_outcome is not FailureClass.SUCCESS:
                    raise RouterError(
                        "router_upstream_incomplete_stream",
                        502,
                        "The upstream stream ended without a completion event.",
                    )
                finish(FailureClass.SUCCESS)
                completion.set_result(
                    ExecutionStats(
                        status="success",
                        total_latency_ms=(time.monotonic() - started) * 1000,
                        time_to_first_event_ms=(first_event_at - started) * 1000,
                        usage=usage.usage,
                    )
                )
                return
            usage.feed(chunk)
            yield chunk
    except asyncio.CancelledError:
        finish(FailureClass.CLIENT_CANCELLED)
        if not completion.done():
            completion.set_result(
                ExecutionStats(
                    status="cancelled",
                    total_latency_ms=(time.monotonic() - started) * 1000,
                    error_code="router_cancelled",
                )
            )
        raise
    except GeneratorExit:
        finish(FailureClass.CLIENT_CANCELLED)
        if not completion.done():
            completion.set_result(
                ExecutionStats(
                    status="cancelled",
                    total_latency_ms=(time.monotonic() - started) * 1000,
                    error_code="router_cancelled",
                )
            )
        raise
    except RouterError as exc:
        finish(FailureClass.POST_COMMIT_STREAM_FAILURE)
        if not completion.done():
            completion.set_result(
                ExecutionStats(
                    status="error",
                    total_latency_ms=(time.monotonic() - started) * 1000,
                    error_code=exc.code,
                )
            )
        yield semantics.render_post_commit_error(exc.code)
    # The committed stream cannot propagate arbitrary iterator failures downstream.
    except Exception:  # noqa: BLE001
        finish(FailureClass.POST_COMMIT_STREAM_FAILURE)
        if not completion.done():
            completion.set_result(
                ExecutionStats(
                    status="error",
                    total_latency_ms=(time.monotonic() - started) * 1000,
                    error_code="router_upstream_read_failed",
                )
            )
        yield semantics.render_post_commit_error("router_upstream_read_failed")
    finally:
        await exchange.close()
