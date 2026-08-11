"""Provider-neutral contracts shared by all application capabilities."""

from tkr_cloud_video.core.clock import Clock, SystemClock
from tkr_cloud_video.core.context import OperationContext, validate_identifier
from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.core.logging import (
    EventSink,
    JsonStreamEventSink,
    StructuredEvent,
    configure_logging,
)
from tkr_cloud_video.core.settings import CoreSettings, LogLevel, SettingsSource

__all__ = [
    "AppError",
    "Clock",
    "CoreSettings",
    "EventSink",
    "JsonStreamEventSink",
    "LogLevel",
    "OperationContext",
    "SettingsSource",
    "StructuredEvent",
    "SystemClock",
    "configure_logging",
    "validate_identifier",
]
