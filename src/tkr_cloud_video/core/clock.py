"""Time ports and the production standard-library adapter."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    """Provider-neutral source of wall and monotonic time."""

    def now(self) -> datetime:
        """Return a timezone-aware current time."""
        ...

    def monotonic(self) -> float:
        """Return a monotonic duration source in seconds."""
        ...


class SystemClock:
    """Production clock backed by the Python standard library."""

    def now(self) -> datetime:
        """Return the current UTC time."""
        return datetime.now(tz=UTC)

    def monotonic(self) -> float:
        """Return the current monotonic clock reading."""
        return time.monotonic()
