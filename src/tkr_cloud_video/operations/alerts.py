"""Typed threshold alerts linked to operator runbooks."""

from __future__ import annotations

from dataclasses import dataclass

from tkr_cloud_video.operations.events import Phase, Severity


@dataclass(frozen=True, slots=True)
class AlertRule:
    """One safe metric threshold and runbook reference."""

    name: str
    metric: str
    threshold: float
    evaluation_periods: int
    severity: Severity
    phase: Phase
    runbook: str

    def __post_init__(self) -> None:
        """Reject thresholds that can trigger without durable evidence."""
        if self.evaluation_periods < 1:
            raise ValueError("evaluation_periods must be positive")
        if not self.runbook.startswith("docs/runbooks/"):
            raise ValueError("runbook must be a repository runbook path")

    def triggered(self, values: tuple[float, ...]) -> bool:
        """Require the configured number of consecutive threshold breaches."""
        if len(values) < self.evaluation_periods:
            return False
        window = values[-self.evaluation_periods :]
        return all(value >= self.threshold for value in window)
