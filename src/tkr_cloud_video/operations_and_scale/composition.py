"""IoC composition for telemetry, Serverless handling, and rollout."""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from tkr_cloud_video.operations.logging import OperationalEventEmitter
from tkr_cloud_video.operations.metrics import MetricsRecorder
from tkr_cloud_video.release.rollout import RolloutPolicy
from tkr_cloud_video.release.runpod_handler import JobApplication, RunPodHandler


@dataclass(frozen=True, slots=True)
class OperationsDependencies:
    """Injected telemetry and shared application dependencies."""

    logger: structlog.typing.FilteringBoundLogger
    metrics: MetricsRecorder
    application: JobApplication
    minimum_readiness: float
    maximum_error_rate: float
    candidate_release_id: str
    rollback_release_id: str


@dataclass(frozen=True, slots=True)
class OperationsServices:
    """Composed observability and Serverless release services."""

    events: OperationalEventEmitter
    metrics: MetricsRecorder
    handler: RunPodHandler
    rollout: RolloutPolicy


def compose_operations_and_scale(
    dependencies: OperationsDependencies,
) -> OperationsServices:
    """Compose operations services without provider globals."""
    return OperationsServices(
        OperationalEventEmitter(dependencies.logger),
        dependencies.metrics,
        RunPodHandler(dependencies.application),
        RolloutPolicy(
            dependencies.minimum_readiness,
            dependencies.maximum_error_rate,
            dependencies.candidate_release_id,
            dependencies.rollback_release_id,
        ),
    )
