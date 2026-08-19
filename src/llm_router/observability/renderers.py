"""Safe table and JSON renderers for observation queries."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any


def render_json(kind: str, payload: object, filters: Mapping[str, object]) -> str:
    """Render one versioned compact JSON document."""

    return json.dumps(
        {"schema_version": 1, "kind": kind, "filters": dict(filters), kind: payload},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def render_table(rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> str:
    """Render bounded rows as a compact whitespace table."""

    if not rows:
        return "no observations"
    widths = {
        column: min(48, max(len(column), *(len(_text(row.get(column))) for row in rows)))
        for column in columns
    }
    header = " ".join(column.upper().ljust(widths[column]) for column in columns)
    body = [
        " ".join(_text(row.get(column))[: widths[column]].ljust(widths[column]) for column in columns)
        for row in rows
    ]
    return "\n".join((header, *body))


def render_trace_tree(payload: Mapping[str, Any]) -> str:
    """Render persisted parent relationships without inventing missing spans."""

    gap = payload.get("gap")
    if gap is not None:
        return str(gap)
    spans = payload.get("spans")
    if not isinstance(spans, list):
        return "trace_not_captured"
    span_ids = {str(span["span_id"]) for span in spans}
    lines: list[str] = []
    for span in spans:
        parent = span.get("parent_span_id")
        prefix = "  " if parent in span_ids else ""
        lines.append(
            f"{prefix}{span['name']} {float(span['duration_ms']):.3f}ms {span['status']}"
        )
    return "\n".join(lines)


def _text(value: object) -> str:
    """Format nullable scalar values without exposing object representations."""

    return "-" if value is None else str(value)
