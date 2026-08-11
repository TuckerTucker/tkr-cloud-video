"""Injected bounded-cardinality metrics adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.operations.events import SAFE_METRIC_LABELS, MetricName


class TelemetryContractError(AppError):
    """A metric violates the bounded-cardinality telemetry contract."""


class MetricsRecorder(Protocol):
    """Provider-neutral counter/histogram port."""

    def increment(
        self, name: MetricName, labels: dict[str, str], value: int = 1
    ) -> None:
        """Increment a bounded counter."""
        ...

    def observe(self, name: MetricName, labels: dict[str, str], value: float) -> None:
        """Observe a bounded histogram value."""
        ...


@dataclass
class InMemoryMetrics(MetricsRecorder):
    """Deterministic metrics recorder and contract reference adapter."""

    counters: dict[tuple[MetricName, tuple[tuple[str, str], ...]], int] = field(
        default_factory=dict
    )
    observations: list[tuple[MetricName, tuple[tuple[str, str], ...], float]] = field(
        default_factory=list
    )

    def _labels(self, labels: dict[str, str]) -> tuple[tuple[str, str], ...]:
        unknown = set(labels).difference(SAFE_METRIC_LABELS)
        if unknown:
            raise TelemetryContractError(
                "metric_label_invalid", "Metric label is not allowlisted."
            )
        if any(len(value) > 128 for value in labels.values()):
            raise TelemetryContractError(
                "metric_label_invalid", "Metric label value is too long."
            )
        return tuple(sorted(labels.items()))

    def increment(
        self, name: MetricName, labels: dict[str, str], value: int = 1
    ) -> None:
        """Increment a counter after label enforcement."""
        if value < 0:
            raise ValueError("metric counter increments cannot be negative")
        key = (name, self._labels(labels))
        self.counters[key] = self.counters.get(key, 0) + value

    def observe(self, name: MetricName, labels: dict[str, str], value: float) -> None:
        """Record a non-negative observation after label enforcement."""
        if value < 0:
            raise ValueError("metric observations cannot be negative")
        self.observations.append((name, self._labels(labels), value))
