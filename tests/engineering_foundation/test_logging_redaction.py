"""Security tests for the structured logging boundary."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime

import pytest

from tests.conftest import CapturingEventSink
from tkr_cloud_video.core.errors import EventValidationError, TelemetryDeliveryError
from tkr_cloud_video.core.logging import (
    JsonStreamEventSink,
    StructuredEvent,
    configure_logging,
    event_to_json,
)
from tkr_cloud_video.core.settings import LogLevel


def test_logger_delivers_validated_correlated_event() -> None:
    """The injected sink observes a typed allowlisted event."""
    sink = CapturingEventSink()
    logger = configure_logging(sink, LogLevel.DEBUG)

    logger.info(
        "engineering.core.compose.succeeded",
        correlation_id="correlation-1",
        outcome="succeeded",
        duration_ms=2.5,
    )

    assert len(sink.events) == 1
    assert sink.events[0].correlation_id == "correlation-1"
    assert sink.events[0].level == "INFO"


@pytest.mark.parametrize(
    "field",
    ["authorization", "credential", "password", "prompt", "signed_url", "token"],
)
def test_logger_rejects_forbidden_fields_before_delivery(field: str) -> None:
    """Sensitive fields never reach the injected event sink."""
    sink = CapturingEventSink()
    logger = configure_logging(sink)

    with pytest.raises(EventValidationError):
        logger.info(
            "engineering.event.rejected",
            correlation_id="correlation-1",
            **{field: "synthetic-secret-marker"},
        )
    assert sink.events == []


def test_logger_rejects_nested_forbidden_context() -> None:
    """Structural validation also examines keys nested in safe_context."""
    sink = CapturingEventSink()
    logger = configure_logging(sink)

    with pytest.raises(EventValidationError):
        logger.error(
            "engineering.event.rejected",
            correlation_id="correlation-1",
            safe_context={"access_token": "synthetic-secret-marker"},
        )
    assert sink.events == []


def test_sink_failure_is_typed_without_payload_fallback() -> None:
    """Telemetry failures remain retryable and do not print event data."""

    class FailingSink:
        def emit(self, _event: StructuredEvent) -> None:
            raise OSError("synthetic-secret-marker")

    logger = configure_logging(FailingSink())
    with pytest.raises(TelemetryDeliveryError) as captured:
        logger.error(
            "engineering.core.compose.failed",
            correlation_id="correlation-1",
            outcome="failed",
        )

    assert captured.value.retryable is True
    assert "synthetic-secret-marker" not in str(captured.value.to_safe_dict())


def test_json_sink_and_canonical_serializer() -> None:
    """JSON outputs contain schema data and omit absent optional fields."""
    stream = io.StringIO()
    event = StructuredEvent(
        event="engineering.doctor.completed",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        level="INFO",
        correlation_id="correlation-1",
        outcome="succeeded",
    )

    JsonStreamEventSink(stream).emit(event)
    streamed = json.loads(stream.getvalue())
    canonical = json.loads(event_to_json(event))

    assert streamed == canonical
    assert "error_code" not in streamed
