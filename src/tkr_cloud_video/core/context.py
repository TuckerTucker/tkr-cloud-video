"""Immutable operation identity and deadline values."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from tkr_cloud_video.core.errors import ContextValidationError

IDENTIFIER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$"
)


def validate_identifier(value: str, field: str) -> str:
    """Validate a bounded opaque identifier.

    Args:
        value: Candidate identifier.
        field: Safe schema field name for diagnostics.

    Returns:
        The unchanged validated identifier.

    Raises:
        ContextValidationError: If the value is empty, unsafe, or too long.

    """
    if not IDENTIFIER_PATTERN.fullmatch(value):
        raise ContextValidationError(
            "invalid_identifier",
            "Identifier contains unsupported characters or length.",
            context={"field": field},
        )
    return value


@dataclass(frozen=True, slots=True)
class OperationContext:
    """Correlates one bounded operation without ambient global state."""

    release_id: str
    worker_id: str
    correlation_id: str
    deadline: datetime
    job_id: str | None = None
    attempt_id: str | None = None

    def __post_init__(self) -> None:
        """Reject unsafe identities and ambiguous deadlines at construction."""
        for field in ("release_id", "worker_id", "correlation_id"):
            validate_identifier(str(getattr(self, field)), field)
        for field in ("job_id", "attempt_id"):
            value = getattr(self, field)
            if value is not None:
                validate_identifier(value, field)
        if self.deadline.tzinfo is None or self.deadline.utcoffset() is None:
            raise ContextValidationError(
                "naive_deadline",
                "Operation deadline must include a timezone.",
                context={"field": "deadline"},
            )
        if self.attempt_id is not None and self.job_id is None:
            raise ContextValidationError(
                "orphan_attempt",
                "An attempt identifier requires a job identifier.",
                context={"field": "attempt_id"},
            )

    def log_context(self) -> dict[str, str]:
        """Return the allowlisted correlation fields for structured events."""
        values = {
            "release_id": self.release_id,
            "worker_id": self.worker_id,
            "correlation_id": self.correlation_id,
            "job_id": self.job_id,
            "attempt_id": self.attempt_id,
        }
        return {key: value for key, value in values.items() if value is not None}
