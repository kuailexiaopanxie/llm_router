"""Public dashboard read use cases over persisted observations."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from llm_router.dashboard.models import (
    DashboardFilters,
    DashboardRuntimeSnapshot,
    OverviewSnapshot,
    RequestDetail,
    RequestPage,
    RequestPageQuery,
)
from llm_router.dashboard.query_overview import build_overview
from llm_router.dashboard.query_requests import build_request_detail, build_request_page
from llm_router.dashboard.sqlite_reader import DashboardSQLiteReader


class DashboardQuery:
    """Build bounded dashboard read models from persisted observations."""

    def __init__(self, reader: DashboardSQLiteReader, runtime_source: object | None = None) -> None:
        """Bind read-only history and an optional sanitized runtime source."""

        self._reader = reader
        self._runtime_source = runtime_source

    def overview(self, filters: DashboardFilters) -> OverviewSnapshot:
        """Return one consistent historical Overview snapshot."""

        with self._reader.transaction() as connection:
            capabilities = self._reader.capabilities(connection)
            payload = build_overview(connection, filters, capabilities)
        runtime: DashboardRuntimeSnapshot | None = None
        try:
            if self._runtime_source is not None:
                runtime = self._runtime_source.snapshot()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - live status must not discard history.
            runtime = None
        if runtime is not None:
            payload["current_health"] = list(runtime.targets)
        else:
            payload["current_health"] = {"status": "unavailable"}
        payload.update({"schema_version": 1, "generated_at": datetime.now(UTC),
                        "runtime": runtime if runtime is not None else {"status": "unavailable"}})
        return OverviewSnapshot(payload)

    def requests(self, query: RequestPageQuery) -> RequestPage:
        """Return one stable keyset page of terminal requests."""

        with self._reader.transaction() as connection:
            capabilities = self._reader.capabilities(connection)
            payload = build_request_page(connection, query, capabilities)
        payload.update({"schema_version": 1, "generated_at": datetime.now(UTC), "filters": query.filters})
        return RequestPage(payload)

    def request_detail(self, request_id: UUID) -> RequestDetail | None:
        """Return one consistent request detail or none when absent."""

        with self._reader.transaction() as connection:
            capabilities = self._reader.capabilities(connection)
            payload = build_request_detail(connection, request_id, capabilities)
        if payload is None:
            return None
        payload.update({"schema_version": 1, "generated_at": datetime.now(UTC)})
        return RequestDetail(payload)
