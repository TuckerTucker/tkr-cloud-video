"""Bounded exposure rollout and deterministic rollback policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RolloutDecision(StrEnum):
    """Stable release pipeline actions."""

    HOLD = "hold"
    PROMOTE = "promote"
    ROLLBACK = "rollback"


@dataclass(frozen=True, slots=True)
class RolloutEvaluation:
    """Auditable action and target selected by the rollout gate."""

    decision: RolloutDecision
    target_release_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class RolloutPolicy:
    """Readiness and error thresholds for bounded exposure."""

    minimum_readiness: float
    maximum_error_rate: float
    candidate_release_id: str
    rollback_release_id: str

    def __post_init__(self) -> None:
        """Validate bounded ratios and immutable release identities."""
        if not 0 <= self.minimum_readiness <= 1:
            raise ValueError("minimum_readiness must be a ratio")
        if not 0 <= self.maximum_error_rate <= 1:
            raise ValueError("maximum_error_rate must be a ratio")
        if self.candidate_release_id == self.rollback_release_id:
            raise ValueError("candidate and rollback releases must differ")

    def evaluate(
        self, readiness: float, error_rate: float, acceptance_passed: bool
    ) -> RolloutEvaluation:
        """Promote only passing candidates and rollback breached live releases."""
        if not acceptance_passed:
            return RolloutEvaluation(
                RolloutDecision.HOLD,
                self.rollback_release_id,
                "acceptance_incomplete",
            )
        if readiness < self.minimum_readiness or error_rate > self.maximum_error_rate:
            return RolloutEvaluation(
                RolloutDecision.ROLLBACK,
                self.rollback_release_id,
                "operational_threshold_breached",
            )
        return RolloutEvaluation(
            RolloutDecision.PROMOTE,
            self.candidate_release_id,
            "all_gates_passed",
        )
