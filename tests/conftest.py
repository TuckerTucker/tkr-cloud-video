"""Deterministic shared test adapters."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from tkr_cloud_video.core.logging import StructuredEvent


@dataclass(frozen=True)
class FakeClock:
    """Stable clock used by unit tests."""

    current: datetime = datetime(2026, 1, 1, tzinfo=UTC)
    monotonic_value: float = 10.0

    def now(self) -> datetime:
        """Return a fixed UTC instant."""
        return self.current

    def monotonic(self) -> float:
        """Return a fixed monotonic reading."""
        return self.monotonic_value


@dataclass(frozen=True)
class FakeSettingsSource:
    """Mapping-backed settings adapter."""

    values: Mapping[str, str]

    def load(self) -> Mapping[str, str]:
        """Return the configured test values."""
        return dict(self.values)


@dataclass
class CapturingEventSink:
    """In-memory event sink for contract assertions."""

    events: list[StructuredEvent] = field(default_factory=list)

    def emit(self, event: StructuredEvent) -> None:
        """Capture one validated event."""
        self.events.append(event)
