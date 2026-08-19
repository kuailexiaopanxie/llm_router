"""v0.7 trace context and first-terminal lifecycle tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from llm_router.observability.lifecycle import (
    ActiveObservationRegistry,
    RequestObservation,
)
from llm_router.observability.metrics import RouterMetrics
from llm_router.observability.models import (
    CostEstimate,
    CostStatus,
    EndpointKind,
    RequestStatus,
    TerminalStage,
)
from llm_router.observability.tracing import TraceBuilder, sampled, trace_context


def test_traceparent_is_strict_and_sampling_is_deterministic() -> None:
    """Accept strict W3C v00 values and ignore malformed parents."""

    raw = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
    accepted = trace_context(raw)
    rejected = trace_context(raw.upper())
    assert accepted.source == "remote_parent"
    assert accepted.parent_span_id == "0123456789abcdef"
    assert rejected.source == "generated"
    assert sampled(accepted.trace_id, Decimal(0)) is False
    assert sampled(accepted.trace_id, Decimal(1)) is True


def test_lifecycle_accepts_only_first_terminal_and_reclaims_gauge() -> None:
    """Emit one immutable terminal fact and count duplicate completion."""

    events: list[object] = []
    metrics = RouterMetrics()
    lifecycle = RequestObservation(
        SimpleNamespace(record=events.append),
        uuid4(),
        trace_context(None),
        datetime.now(UTC),
        EndpointKind.MESSAGES,
        metrics,
    )
    assert lifecycle.finish(RequestStatus.ERROR, TerminalStage.AUTHENTICATION, "router_unauthorized")
    assert not lifecycle.finish(RequestStatus.SUCCESS, TerminalStage.COMPLETED)
    rendered = metrics.render().decode()
    assert len(events) == 1
    assert 'llm_router_inflight_requests{protocol="unknown"} 0.0' in rendered
    assert "llm_router_observation_duplicate_terminal_total 1.0" in rendered


def test_trace_builder_creates_root_only_for_auth_failure() -> None:
    """Build only a root span before routing begins."""

    events: list[object] = []
    lifecycle = RequestObservation(
        SimpleNamespace(record=events.append),
        uuid4(),
        trace_context(None),
        datetime.now(UTC),
        EndpointKind.RESPONSES,
    )
    lifecycle.finish(RequestStatus.ERROR, TerminalStage.AUTHENTICATION, "router_unauthorized")
    spans = TraceBuilder(Decimal(1)).build(
        events[0], CostEstimate(CostStatus.NOT_APPLICABLE, None, None, None)
    )
    assert [span.name for span in spans] == ["llm_router.request"]
    assert spans[0].attributes["llm_router.cost_status"] == "not_applicable"


def test_active_registry_marks_shutdown_requests_abandoned() -> None:
    """Finish every remaining active lifecycle during shutdown."""

    events: list[object] = []
    registry = ActiveObservationRegistry(SimpleNamespace(record=events.append), RouterMetrics())
    registry.open(uuid4(), trace_context(None), datetime.now(UTC), EndpointKind.MESSAGES)
    assert registry.abandon_all() == 1
    assert events[0].status is RequestStatus.ABANDONED  # type: ignore[union-attr]
