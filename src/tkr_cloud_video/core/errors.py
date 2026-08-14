"""Safe, typed application errors."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

SAFE_CONTEXT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "attempt_id",
        "correlation_id",
        "error_type",
        "field",
        "gate",
        "job_id",
        "operation",
        "relative_path",
        "resource_id",
        "rule",
        "worker_id",
    }
)
FORBIDDEN_KEY_PARTS: Final[tuple[str, ...]] = (
    "authorization",
    "credential",
    "filename",
    "password",
    "prompt",
    "secret",
    "signed_url",
    "token",
)


class AppError(Exception):
    """Base error that exposes only explicitly safe diagnostic information.

    Args:
        code: Stable machine-readable error code.
        safe_message: Message suitable for logs and callers.
        retryable: Whether an unchanged operation may succeed later.
        context: Allowlisted scalar diagnostic fields.
        cause: Wrapped internal exception, deliberately omitted from serialization.

    """

    def __init__(
        self,
        code: str,
        safe_message: str,
        *,
        retryable: bool = False,
        context: Mapping[str, str | int | float | bool | None] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        """Initialize an error after validating its externally visible data."""
        validated_context = _validate_context(context or {})
        self.code = code
        self.safe_message = safe_message
        self.retryable = retryable
        self.context = MappingProxyType(validated_context)
        self.__cause__ = cause
        super().__init__(safe_message)

    def to_safe_dict(self) -> dict[str, object]:
        """Return the stable serialization contract without the wrapped cause."""
        return {
            "code": self.code,
            "message": self.safe_message,
            "retryable": self.retryable,
            "context": dict(self.context),
        }


class ContextValidationError(AppError):
    """An operation context value is invalid."""


class SettingsValidationError(AppError):
    """External settings do not satisfy the typed schema."""


class EventValidationError(AppError):
    """A structured event contains unsafe or unknown data."""


class TelemetryDeliveryError(AppError):
    """An injected telemetry sink could not accept an event."""


class BootstrapError(AppError):
    """The dependency composition root could not be built."""


class DoctorCheckError(AppError):
    """An offline diagnostic contract failed."""


def _validate_context(
    context: Mapping[str, str | int | float | bool | None],
) -> dict[str, str | int | float | bool | None]:
    """Validate safe error context before it crosses an observability boundary."""
    unknown = set(context).difference(SAFE_CONTEXT_KEYS)
    forbidden = {
        key
        for key in context
        if any(part in key.lower() for part in FORBIDDEN_KEY_PARTS)
    }
    if unknown or forbidden:
        raise ValueError("error context contains a non-allowlisted field")
    return dict(context)
