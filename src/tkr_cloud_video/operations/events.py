"""Typed cross-capability telemetry event vocabulary."""

from __future__ import annotations

from enum import StrEnum


class EventName(StrEnum):
    """Allowlisted operational state-transition event names."""

    BOOT = "operations.worker.boot"
    HYDRATION = "operations.worker.hydration"
    READINESS = "operations.worker.readiness"
    GENERATION = "operations.job.generation"
    UPLOAD = "operations.delivery.upload"
    COMMIT = "operations.delivery.commit"
    DELIVERY = "operations.delivery.access"
    AUTHORIZATION = "operations.security.authorization"


class Phase(StrEnum):
    """Correlatable platform phases from boot through delivery."""

    BOOT = "boot"
    HYDRATION = "hydration"
    READINESS = "readiness"
    GENERATION = "generation"
    UPLOAD = "upload"
    COMMIT = "commit"
    DELIVERY = "delivery"
    AUTHORIZATION = "authorization"


class Severity(StrEnum):
    """Operational event/alert severities."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class Outcome(StrEnum):
    """Bounded state transition outcomes."""

    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MetricName(StrEnum):
    """Bounded metric vocabulary required by the product dashboards."""

    STORAGE_BYTES = "storage.bytes"
    EGRESS_BYTES = "egress.bytes"
    HYDRATION_BYTES = "hydration.bytes"
    HYDRATION_SECONDS = "hydration.seconds"
    COLD_START_SECONDS = "cold_start.seconds"
    GENERATION_SECONDS = "generation.seconds"
    UPLOAD_BYTES = "upload.bytes"
    UPLOAD_SECONDS = "upload.seconds"
    ERRORS_TOTAL = "errors.total"
    CACHE_HITS_TOTAL = "cache.hits_total"
    CACHE_MISSES_TOTAL = "cache.misses_total"
    READINESS = "worker.readiness"


SAFE_METRIC_LABELS = frozenset(
    {"environment", "phase", "outcome", "error_code", "release_id", "provider"}
)
