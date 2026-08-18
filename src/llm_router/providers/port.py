"""Provider abstraction used by the execution engine."""

from __future__ import annotations

from typing import Protocol

from llm_router.domain import ProviderExchange, ProviderRequest
from llm_router.health.models import FailureClass


class ProviderFailure(Exception):
    """Sanitized transport failure raised before an exchange is available."""

    def __init__(
        self,
        code: str,
        failure_class: FailureClass,
        retry_after_seconds: float | None = None,
    ) -> None:
        """Store only bounded failure metadata safe for core execution."""

        super().__init__(code)
        self.code = code
        self.failure_class = failure_class
        self.retry_after_seconds = retry_after_seconds


class ProviderPort(Protocol):
    """Open one upstream protocol exchange for a resolved model target."""

    async def invoke(self, request: ProviderRequest) -> ProviderExchange:
        """Open an upstream exchange and expose status, headers, and raw bytes."""
