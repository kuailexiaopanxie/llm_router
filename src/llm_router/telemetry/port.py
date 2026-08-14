"""Persistence port for sanitized telemetry events."""

from __future__ import annotations

from typing import Protocol

from llm_router.domain import RouteEvent


class EventStorePort(Protocol):
    """Persist sanitized route events without exposing storage details."""

    async def start(self) -> None:
        """Initialize the backing store."""

    async def append(self, event: RouteEvent) -> None:
        """Persist one sanitized event."""

    async def close(self) -> None:
        """Close the backing store."""

