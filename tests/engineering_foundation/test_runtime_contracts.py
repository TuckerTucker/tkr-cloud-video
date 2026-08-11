"""Tests for immutable shared runtime contracts."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.conftest import FakeSettingsSource
from tkr_cloud_video.core.context import OperationContext, validate_identifier
from tkr_cloud_video.core.errors import AppError, ContextValidationError
from tkr_cloud_video.core.settings import CoreSettings, LogLevel, load_core_settings


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("worker-01", True),
        ("release.2026_08", True),
        ("", False),
        ("../escape", False),
        ("space separated", False),
        ("x" * 129, False),
    ],
)
def test_identifier_validation(value: str, valid: bool) -> None:
    """Safe opaque identifiers reject path and whitespace syntax."""
    if valid:
        assert validate_identifier(value, "worker_id") == value
    else:
        with pytest.raises(ContextValidationError) as captured:
            validate_identifier(value, "worker_id")
        assert captured.value.code == "invalid_identifier"


def test_operation_context_is_immutable_and_correlated() -> None:
    """Operation identity exposes only present allowlisted correlation fields."""
    context = OperationContext(
        release_id="release-1",
        worker_id="worker-1",
        correlation_id="correlation-1",
        deadline=datetime(2026, 1, 1, tzinfo=UTC),
        job_id="job-1",
        attempt_id="attempt-1",
    )

    assert context.log_context() == {
        "release_id": "release-1",
        "worker_id": "worker-1",
        "correlation_id": "correlation-1",
        "job_id": "job-1",
        "attempt_id": "attempt-1",
    }
    with pytest.raises(AttributeError):
        context.worker_id = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("deadline", "job_id", "attempt_id", "error_code"),
    [
        (datetime(2026, 1, 1), None, None, "naive_deadline"),
        (datetime(2026, 1, 1, tzinfo=UTC), None, "attempt-1", "orphan_attempt"),
    ],
)
def test_operation_context_rejects_ambiguous_state(
    deadline: datetime,
    job_id: str | None,
    attempt_id: str | None,
    error_code: str,
) -> None:
    """Naive deadlines and orphan attempts fail at construction."""
    with pytest.raises(ContextValidationError) as captured:
        OperationContext(
            release_id="release-1",
            worker_id="worker-1",
            correlation_id="correlation-1",
            deadline=deadline,
            job_id=job_id,
            attempt_id=attempt_id,
        )
    assert captured.value.code == error_code


def test_app_error_omits_wrapped_cause() -> None:
    """Safe serialization never exposes the wrapped exception."""
    marker = "synthetic-secret-marker"
    error = AppError(
        "operation_failed",
        "The operation failed safely.",
        retryable=True,
        context={"operation": "unit_test"},
        cause=RuntimeError(marker),
    )

    serialized = str(error.to_safe_dict())
    assert marker not in serialized
    assert error.to_safe_dict()["retryable"] is True
    assert error.__cause__ is not None


def test_app_error_rejects_unknown_context() -> None:
    """Non-allowlisted diagnostic fields cannot cross the error boundary."""
    with pytest.raises(ValueError, match="non-allowlisted"):
        AppError("unsafe", "unsafe", context={"secret_value": "marker"})


def test_settings_load_from_injected_source() -> None:
    """External mappings become immutable typed settings."""
    settings = load_core_settings(
        FakeSettingsSource(
            {"LOG_LEVEL": "DEBUG", "SCHEMA_VERSION": "1", "ENVIRONMENT": "test"}
        )
    )

    assert settings == CoreSettings(
        log_level=LogLevel.DEBUG, schema_version="1", environment="test"
    )


@pytest.mark.parametrize(
    "values",
    [
        {"UNKNOWN": "value"},
        {"LOG_LEVEL": "TRACE"},
        {"SCHEMA_VERSION": "2"},
        {"ENVIRONMENT": "Production"},
    ],
)
def test_settings_reject_unknown_or_unsupported_values(
    values: dict[str, str],
) -> None:
    """Configuration violations become one typed safe error."""
    with pytest.raises(AppError) as captured:
        load_core_settings(FakeSettingsSource(values))
    assert captured.value.code == "invalid_settings"
