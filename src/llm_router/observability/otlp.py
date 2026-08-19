"""Bounded fail-open OpenTelemetry SDK trace export adapter."""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qsl

from llm_router import __version__
from llm_router.observability.config import OtlpConfig
from llm_router.observability.metrics import RouterMetrics
from llm_router.observability.models import TraceSpan


class OtlpTraceExporter:
    """Map neutral spans to SDK spans and export from an independent queue."""

    def __init__(
        self,
        config: OtlpConfig,
        metrics: RouterMetrics,
        exporter_factory: Callable[[], Any] | None = None,
        span_mapper: Callable[[TraceSpan], Any] | None = None,
    ) -> None:
        """Bind validated export configuration without network activity."""

        self._config = config
        self._metrics = metrics
        self._factory = exporter_factory
        self._span_mapper = span_mapper or self._readable_span
        self._queue: asyncio.Queue[TraceSpan] = asyncio.Queue(config.queue_capacity)
        self._worker: asyncio.Task[None] | None = None
        self._exporter: Any = None
        self._stopping = False
        self._logger = logging.getLogger("llm_router.observability")

    async def start(self) -> None:
        """Initialize the SDK exporter and start its bounded worker fail-open."""

        try:
            self._exporter = self._factory() if self._factory else self._sdk_exporter()
        except (ImportError, ModuleNotFoundError, RuntimeError, ValueError):
            self._metrics.trace_export.labels("otlp", "unavailable").inc()
            self._logger.error(
                "OTLP trace exporter is unavailable",
                extra={"event": "otlp_exporter_unavailable"},
            )
            return
        self._worker = asyncio.create_task(self._run(), name="llm-router-otlp-exporter")

    def _sdk_exporter(self) -> Any:
        """Create the official OTLP HTTP exporter with secret headers from env."""

        module = importlib.import_module(
            "opentelemetry.exporter.otlp.proto.http.trace_exporter"
        )
        return module.OTLPSpanExporter(
            endpoint=self._config.endpoint,
            headers=self._headers(),
            timeout=self._config.timeout_seconds,
        )

    def _headers(self) -> Mapping[str, str] | None:
        """Parse optional URL-encoded or comma-separated exporter headers."""

        if self._config.headers_env is None:
            return None
        raw = os.environ.get(self._config.headers_env)
        if not raw:
            return None
        headers = dict(parse_qsl(raw.replace(",", "&"), keep_blank_values=True))
        if not headers or any(
            not key or "\r" in key or "\n" in key or "\r" in value or "\n" in value
            for key, value in headers.items()
        ):
            raise ValueError("OTLP headers are invalid")
        return headers

    def record(self, spans: tuple[TraceSpan, ...]) -> None:
        """Enqueue sampled spans without blocking the request path."""

        if self._worker is None or self._stopping:
            return
        for span in spans:
            try:
                self._queue.put_nowait(span)
            except asyncio.QueueFull:
                self._metrics.trace_export.labels("otlp", "dropped").inc()
                break

    async def _run(self) -> None:
        """Export bounded batches until shutdown drains accepted spans."""

        while not self._stopping or not self._queue.empty():
            try:
                first = await asyncio.wait_for(self._queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue
            batch = [first]
            while len(batch) < self._config.batch_size:
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            await self._export(batch)
            for _ in batch:
                self._queue.task_done()

    async def _export(self, spans: Sequence[TraceSpan]) -> None:
        """Convert and export one batch while suppressing third-party errors."""

        try:
            readable = tuple(self._span_mapper(span) for span in spans)
            result = await asyncio.wait_for(
                asyncio.to_thread(self._exporter.export, readable),
                timeout=self._config.timeout_seconds,
            )
            success = getattr(result, "name", str(result)).upper() == "SUCCESS"
            self._metrics.trace_export.labels(
                "otlp", "success" if success else "failed"
            ).inc(len(spans))
            if not success:
                self._logger.error(
                    "OTLP trace export failed", extra={"event": "otlp_export_failed"}
                )
        except Exception:  # noqa: BLE001 - third-party exporter must remain fail-open.
            self._metrics.trace_export.labels("otlp", "failed").inc(len(spans))
            self._logger.error(
                "OTLP trace export failed", extra={"event": "otlp_export_failed"}
            )

    @staticmethod
    def _readable_span(span: TraceSpan) -> Any:
        """Map one neutral span to the OpenTelemetry SDK read model."""

        resource_module = importlib.import_module("opentelemetry.sdk.resources")
        sdk_trace = importlib.import_module("opentelemetry.sdk.trace")
        trace_api = importlib.import_module("opentelemetry.trace")

        context = trace_api.SpanContext(
            int(span.trace_id, 16),
            int(span.span_id, 16),
            False,
            trace_api.TraceFlags(trace_api.TraceFlags.SAMPLED),
        )
        parent = None
        if span.parent_span_id is not None:
            parent_context = trace_api.SpanContext(
                int(span.trace_id, 16),
                int(span.parent_span_id, 16),
                False,
                trace_api.TraceFlags(trace_api.TraceFlags.SAMPLED),
            )
            parent = trace_api.NonRecordingSpan(parent_context).get_span_context()
        start_ns = int(span.started_at.timestamp() * 1_000_000_000)
        end_ns = start_ns + int(timedelta(milliseconds=span.duration_ms).total_seconds() * 1_000_000_000)
        status = trace_api.Status(
            trace_api.StatusCode.OK
            if span.status in {"success", "plan", "committed"}
            else trace_api.StatusCode.ERROR
        )
        return sdk_trace.ReadableSpan(
            span.name,
            context=context,
            parent=parent,
            resource=resource_module.Resource.create(
                {"service.name": "coding-llm-router", "service.version": __version__}
            ),
            attributes=dict(span.attributes),
            kind=trace_api.SpanKind.INTERNAL,
            status=status,
            start_time=start_ns,
            end_time=end_ns,
        )

    async def close(self) -> None:
        """Drain accepted spans and stop the SDK exporter within finite time."""

        self._stopping = True
        if self._worker is not None:
            try:
                await asyncio.wait_for(self._queue.join(), timeout=5)
            except asyncio.TimeoutError:
                self._metrics.trace_export.labels("otlp", "shutdown_timeout").inc(
                    self._queue.qsize()
                )
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        if self._exporter is not None:
            try:
                await asyncio.to_thread(self._exporter.shutdown)
            except Exception:  # noqa: BLE001 - third-party shutdown must remain fail-open.
                self._metrics.trace_export.labels("otlp", "shutdown_failed").inc()
