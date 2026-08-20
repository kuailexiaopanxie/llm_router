"""GET-only FastAPI transport for dashboard queries and package assets."""

from __future__ import annotations

import json
import logging
import mimetypes
import time
from collections.abc import Callable
from importlib.resources import files
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from llm_router.dashboard.auth import dashboard_authenticated
from llm_router.dashboard.config import DashboardConfig
from llm_router.dashboard.filters import (
    parse_filters,
    parse_request_page,
    validate_query_length,
)
from llm_router.dashboard.metrics import DashboardMetrics
from llm_router.dashboard.models import json_value
from llm_router.dashboard.query import DashboardQuery
from llm_router.dashboard.sqlite_reader import (
    DashboardQueryError,
    DashboardQueryExecutor,
)

_JSON_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}
_CSP = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
_ERRORS = {
    "invalid_filter": (400, "Invalid dashboard filter"), "unauthorized": (401, "Dashboard authentication required"),
    "request_not_found": (404, "Dashboard request was not found"),
    "observation_unavailable": (503, "Dashboard observation database is unavailable"),
    "unsupported_schema": (503, "Dashboard observation schema is unsupported"),
    "query_timeout": (503, "Dashboard query timed out"), "dashboard_busy": (503, "Dashboard is busy"),
    "response_too_large": (503, "Dashboard response limit exceeded"),
}


def _error(code: str) -> JSONResponse:
    """Render one fixed bounded dashboard error response."""

    status, message = _ERRORS[code]
    return JSONResponse({"error": {"code": code, "message": message, "request_id": str(uuid4())}}, status_code=status, headers=_JSON_HEADERS)


def _json(payload: object) -> Response:
    """Serialize JSON once and enforce the fixed 2 MiB response bound."""

    content = json.dumps(json_value(payload), separators=(",", ":"), ensure_ascii=True).encode()
    if len(content) > 2 * 1024 * 1024:
        return _error("response_too_large")
    return Response(content, media_type="application/json", headers=_JSON_HEADERS)


def _authorized(request: Request, config: DashboardConfig, token: str, metrics: DashboardMetrics) -> bool:
    """Apply configured Bearer authentication and fixed metrics labels."""

    accepted = not config.require_auth or dashboard_authenticated(request.headers, token)
    metrics.auth.labels("success" if accepted else "failure").inc()
    return accepted


async def _run(
    view: str,
    executor: DashboardQueryExecutor,
    metrics: DashboardMetrics,
    function: Callable[[], Any],
) -> Response:
    """Measure and map one isolated synchronous dashboard query."""

    started = time.monotonic()
    metrics.active.inc()
    try:
        result = await executor.run(function)
        metrics.queries.labels(view, "success").inc()
        return _json(result.payload if result is not None else None)
    except DashboardQueryError as exc:
        metrics.queries.labels(view, exc.code).inc()
        return _error(exc.code)
    finally:
        metrics.active.dec()
        metrics.duration.labels(view).observe(time.monotonic() - started)


def _asset(asset_path: str, head: bool = False) -> Response:
    """Resolve a fixed package asset without allowing path traversal."""

    path = PurePosixPath(asset_path)
    if not asset_path or path.is_absolute() or ".." in path.parts or "\0" in asset_path:
        return Response(status_code=404)
    resource = files("llm_router.dashboard.assets").joinpath(*path.parts)
    if not resource.is_file():
        logging.getLogger("llm_router.dashboard").warning("Dashboard static asset is missing", extra={"event": "dashboard_asset_missing"})
        return Response(status_code=404)
    media_type = mimetypes.guess_type(asset_path)[0] or "application/octet-stream"
    headers = {"Cache-Control": "public, max-age=3600", "X-Content-Type-Options": "nosniff"}
    return Response(b"" if head else resource.read_bytes(), media_type=media_type, headers=headers)


def register_dashboard_routes(
    app: object, query: DashboardQuery, executor: DashboardQueryExecutor,
    config: DashboardConfig, token: str, metrics: DashboardMetrics,
) -> None:
    """Register the complete versioned read-only Dashboard namespace."""

    router = APIRouter()

    @router.api_route("/admin", methods=["GET", "HEAD"], response_class=HTMLResponse)
    @router.api_route("/admin/", methods=["GET", "HEAD"], response_class=HTMLResponse)
    @router.api_route("/admin/requests/{request_id}", methods=["GET", "HEAD"], response_class=HTMLResponse)
    async def shell(request: Request, request_id: str | None = None) -> Response:
        """Serve the same static application shell for every browser view."""

        if request_id is not None:
            try:
                UUID(request_id)
            except ValueError:
                return Response(status_code=404)
        response = _asset("index.html", request.method == "HEAD")
        response.headers["Cache-Control"] = "no-cache"
        response.headers["Content-Security-Policy"] = _CSP
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @router.api_route("/admin/assets/{asset_path:path}", methods=["GET", "HEAD"])
    async def assets(request: Request, asset_path: str) -> Response:
        """Serve one local module, stylesheet, icon, or license file."""

        return _asset(asset_path, request.method == "HEAD")

    @router.api_route("/admin/api/v1/overview", methods=["GET", "HEAD"])
    async def overview(request: Request) -> Response:
        """Parse and execute one authenticated Overview query."""

        if not _authorized(request, config, token, metrics):
            return _error("unauthorized")
        try:
            validate_query_length(request.scope.get("query_string", b""))
            filters = parse_filters(request.query_params, config)
        except ValueError:
            return _error("invalid_filter")
        return await _run("overview", executor, metrics, lambda: query.overview(filters))

    @router.api_route("/admin/api/v1/requests", methods=["GET", "HEAD"])
    async def requests(request: Request) -> Response:
        """Parse and execute one authenticated Requests page query."""

        if not _authorized(request, config, token, metrics):
            return _error("unauthorized")
        try:
            validate_query_length(request.scope.get("query_string", b""))
            page = parse_request_page(request.query_params, config)
        except ValueError:
            return _error("invalid_filter")
        return await _run("requests", executor, metrics, lambda: query.requests(page))

    @router.api_route("/admin/api/v1/requests/{request_id}", methods=["GET", "HEAD"])
    async def detail(request: Request, request_id: str) -> Response:
        """Execute one authenticated request-detail query."""

        if not _authorized(request, config, token, metrics):
            return _error("unauthorized")
        if request.query_params:
            return _error("invalid_filter")
        try:
            identifier = UUID(request_id)
        except ValueError:
            return _error("invalid_filter")
        response = await _run("detail", executor, metrics, lambda: query.request_detail(identifier))
        if response.status_code == 200 and response.body == b"null":
            return _error("request_not_found")
        return response

    app.include_router(router)  # type: ignore[attr-defined]
