"""Public contracts for the tkr-cloud-video application."""

from tkr_cloud_video.bootstrap import DoctorResult, RuntimeServices, build_runtime
from tkr_cloud_video.core import (
    AppError,
    Clock,
    CoreSettings,
    EventSink,
    OperationContext,
    SettingsSource,
    StructuredEvent,
)

__all__ = [
    "AppError",
    "Clock",
    "CoreSettings",
    "DoctorResult",
    "EventSink",
    "OperationContext",
    "RuntimeServices",
    "SettingsSource",
    "StructuredEvent",
    "build_runtime",
]

__version__ = "0.1.0"
