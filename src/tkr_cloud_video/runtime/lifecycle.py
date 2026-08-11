"""Explicit truthful worker lifecycle state machine."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from tkr_cloud_video.core.errors import AppError


class WorkerState(StrEnum):
    """Worker startup, service, and termination phases."""

    CONFIGURING = "configuring"
    PREFLIGHTING = "preflighting"
    HYDRATING = "hydrating"
    STARTING = "starting"
    VALIDATING = "validating"
    READY = "ready"
    DRAINING = "draining"
    FAILED = "failed"
    STOPPED = "stopped"


TRANSITIONS = {
    WorkerState.CONFIGURING: {WorkerState.PREFLIGHTING, WorkerState.FAILED},
    WorkerState.PREFLIGHTING: {WorkerState.HYDRATING, WorkerState.FAILED},
    WorkerState.HYDRATING: {WorkerState.STARTING, WorkerState.FAILED},
    WorkerState.STARTING: {WorkerState.VALIDATING, WorkerState.FAILED},
    WorkerState.VALIDATING: {WorkerState.READY, WorkerState.FAILED},
    WorkerState.READY: {WorkerState.DRAINING, WorkerState.FAILED},
    WorkerState.DRAINING: {WorkerState.STOPPED, WorkerState.FAILED},
    WorkerState.FAILED: {WorkerState.STOPPED},
    WorkerState.STOPPED: set(),
}


@dataclass
class WorkerLifecycle:
    """Rejects skipped phases and derives admission from exact state."""

    state: WorkerState = WorkerState.CONFIGURING
    failure_code: str | None = None

    @property
    def ready(self) -> bool:
        """Return whether the worker may admit jobs."""
        return self.state is WorkerState.READY

    @property
    def live(self) -> bool:
        """Return whether the supervisor can continue serving."""
        return self.state not in {WorkerState.FAILED, WorkerState.STOPPED}

    def transition(self, target: WorkerState) -> None:
        """Advance through one legal lifecycle edge."""
        if target not in TRANSITIONS[self.state]:
            raise AppError(
                "invalid_worker_transition", "Worker state transition is invalid."
            )
        self.state = target

    def fail(self, code: str) -> None:
        """Record one terminal safe failure reason."""
        if self.state is not WorkerState.FAILED:
            self.transition(WorkerState.FAILED)
        self.failure_code = code
