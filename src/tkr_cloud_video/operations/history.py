"""Bounded queryable history for already validated operational events."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from tkr_cloud_video.core.logging import EventSink, StructuredEvent


@dataclass
class OperationalEventHistory(EventSink):
    """Retain an ordered bounded window without high-cardinality indexing."""

    capacity: int = 10_000
    _events: deque[StructuredEvent] = field(init=False)

    def __post_init__(self) -> None:
        """Create the bounded store after validating its capacity."""
        if self.capacity < 1:
            raise ValueError("event history capacity must be positive")
        self._events = deque(maxlen=self.capacity)

    def emit(self, event: StructuredEvent) -> None:
        """Append one validated event, evicting only the oldest event."""
        self._events.append(event)

    def for_correlation(self, correlation_id: str) -> tuple[StructuredEvent, ...]:
        """Return matching events in delivery order."""
        return tuple(
            event for event in self._events if event.correlation_id == correlation_id
        )
