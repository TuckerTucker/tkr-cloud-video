"""Validated structlog boundary with structural secret exclusion."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Mapping, MutableMapping
from datetime import UTC, datetime
from typing import Any, Final, Protocol, TextIO, cast

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from structlog.typing import EventDict, Processor

from tkr_cloud_video.core.context import validate_identifier
from tkr_cloud_video.core.errors import (
    FORBIDDEN_KEY_PARTS,
    EventValidationError,
    TelemetryDeliveryError,
)
from tkr_cloud_video.core.settings import LogLevel

ALLOWED_EVENT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "attempt_id",
        "correlation_id",
        "duration_ms",
        "error_code",
        "event",
        "job_id",
        "level",
        "lock_digest",
        "outcome",
        "python_version",
        "release_id",
        "resource_id",
        "safe_context",
        "scenario_id",
        "timestamp",
        "worker_id",
    }
)


class StructuredEvent(BaseModel):
    """Immutable, allowlisted event delivered to an injected sink."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event: str
    timestamp: datetime
    level: str
    correlation_id: str
    duration_ms: float | None = Field(default=None, ge=0)
    outcome: str | None = None
    error_code: str | None = None
    release_id: str | None = None
    worker_id: str | None = None
    job_id: str | None = None
    attempt_id: str | None = None
    resource_id: str | None = None
    scenario_id: str | None = None
    python_version: str | None = None
    lock_digest: str | None = None
    safe_context: Mapping[str, str | int | float | bool | None] = Field(
        default_factory=dict
    )

    @field_validator("event")
    @classmethod
    def validate_event_name(cls, value: str) -> str:
        """Require a namespaced event identifier."""
        validate_identifier(value, "event")
        if "." not in value:
            raise ValueError("event must be namespaced")
        return value

    @field_validator(
        "correlation_id",
        "release_id",
        "worker_id",
        "job_id",
        "attempt_id",
        "resource_id",
        "scenario_id",
    )
    @classmethod
    def validate_optional_identifiers(cls, value: str | None) -> str | None:
        """Validate every correlation identity using the shared primitive."""
        if value is not None:
            validate_identifier(value, "event_identifier")
        return value

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        """Reject naive event times."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include a timezone")
        return value

    @field_validator("safe_context")
    @classmethod
    def validate_safe_context(
        cls, value: Mapping[str, str | int | float | bool | None]
    ) -> Mapping[str, str | int | float | bool | None]:
        """Reject sensitive-looking keys before an event reaches a sink."""
        for key in value:
            if any(part in key.lower() for part in FORBIDDEN_KEY_PARTS):
                raise ValueError("safe_context contains a forbidden key")
        return dict(value)


class EventSink(Protocol):
    """Port for delivery of already-validated structured events."""

    def emit(self, event: StructuredEvent) -> None:
        """Deliver one validated event."""
        ...


class JsonStreamEventSink:
    """Write one canonical JSON object per line to a text stream."""

    def __init__(self, stream: TextIO) -> None:
        """Initialize the sink with an explicitly owned output stream."""
        self._stream = stream

    def emit(self, event: StructuredEvent) -> None:
        """Serialize a validated event as JSON without fallback repr output."""
        self._stream.write(event.model_dump_json(exclude_none=True) + "\n")
        self._stream.flush()


class _SinkProxy:
    """No-op logger target; delivery occurs in the validation processor."""

    def debug(self, *_args: object, **_kwargs: object) -> None:
        return None

    info = debug
    warning = debug
    error = debug
    exception = debug
    critical = debug


def _validation_processor(sink: EventSink) -> Processor:
    """Build a processor that validates and delivers one event."""

    def process(
        _logger: object, method_name: str, event_dict: MutableMapping[str, Any]
    ) -> EventDict:
        candidate = dict(event_dict)
        candidate["level"] = method_name.upper()
        candidate.setdefault("timestamp", datetime.now(tz=UTC))
        unknown = set(candidate).difference(ALLOWED_EVENT_KEYS)
        forbidden = {
            key
            for key in candidate
            if any(part in key.lower() for part in FORBIDDEN_KEY_PARTS)
        }
        if unknown or forbidden:
            raise EventValidationError(
                "event_schema_violation",
                "Structured event contains an unsupported field.",
                context={"operation": "emit_event"},
            )
        try:
            event = StructuredEvent.model_validate(candidate)
        except (ValidationError, ValueError) as error:
            raise EventValidationError(
                "event_schema_violation",
                "Structured event failed schema validation.",
                context={"operation": "emit_event"},
                cause=error,
            ) from error
        try:
            sink.emit(event)
        except Exception as error:
            raise TelemetryDeliveryError(
                "event_sink_unavailable",
                "Structured event sink is unavailable.",
                retryable=True,
                context={"operation": "emit_event"},
                cause=error,
            ) from error
        return cast(EventDict, candidate)

    return process


def configure_logging(
    sink: EventSink,
    level: LogLevel = LogLevel.INFO,
) -> structlog.typing.FilteringBoundLogger:
    """Create an isolated structlog logger that delivers only validated events.

    This does not mutate structlog's global defaults, so tests and composition
    roots can own independent sinks.

    Args:
        sink: Destination for validated structured events.
        level: Minimum event severity.

    Returns:
        A locally configured bound structlog logger.

    """
    wrapper = structlog.make_filtering_bound_logger(logging.getLevelName(level.value))
    return cast(
        structlog.typing.FilteringBoundLogger,
        structlog.wrap_logger(
            _SinkProxy(),
            processors=[_validation_processor(sink)],
            wrapper_class=wrapper,
            cache_logger_on_first_use=True,
        ),
    )


def event_to_json(event: StructuredEvent) -> str:
    """Return deterministic JSON for a validated event."""
    return json.dumps(
        event.model_dump(mode="json", exclude_none=True),
        separators=(",", ":"),
        sort_keys=True,
    )


def default_event_sink() -> JsonStreamEventSink:
    """Construct the CLI's explicit stderr event sink."""
    return JsonStreamEventSink(sys.stderr)
