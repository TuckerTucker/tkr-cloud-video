"""Cross-capability correlated structured event emitter."""

from __future__ import annotations

import structlog

from tkr_cloud_video.core.context import OperationContext
from tkr_cloud_video.operations.events import EventName, Outcome, Phase, Severity


class OperationalEventEmitter:
    """Adds common correlation and bounded phase fields to validated core logging."""

    def __init__(self, logger: structlog.typing.FilteringBoundLogger) -> None:
        """Initialize with the already redacting core structlog boundary."""
        self._logger = logger

    def emit(
        self,
        name: EventName,
        context: OperationContext,
        phase: Phase,
        outcome: Outcome,
        severity: Severity = Severity.INFO,
        *,
        duration_ms: float | None = None,
        error_code: str | None = None,
    ) -> None:
        """Emit one safe state transition without payload data."""
        methods = {
            Severity.INFO: self._logger.info,
            Severity.WARNING: self._logger.warning,
            Severity.CRITICAL: self._logger.error,
        }
        method = methods[severity]
        method(
            name.value,
            **context.log_context(),
            safe_context={"phase": phase.value},
            outcome=outcome.value,
            duration_ms=duration_ms,
            error_code=error_code,
        )
