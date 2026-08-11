"""IoC composition for worker preflight and supervised readiness."""

from __future__ import annotations

from dataclasses import dataclass

from tkr_cloud_video.runtime.comfy_health import ComfyApi, ComfyHealthValidator
from tkr_cloud_video.runtime.lifecycle import WorkerLifecycle
from tkr_cloud_video.runtime.preflight import PlatformProbe, WorkerPreflight
from tkr_cloud_video.runtime.supervisor import (
    ProcessRunner,
    StartupSteps,
    WorkerSupervisor,
)


@dataclass(frozen=True, slots=True)
class WorkerDependencies:
    """External worker platform, process, startup, and ComfyUI ports."""

    platform_probe: PlatformProbe
    process_runner: ProcessRunner
    startup_steps: StartupSteps
    comfy_api: ComfyApi


@dataclass(frozen=True, slots=True)
class WorkerServices:
    """Composed worker services sharing one lifecycle."""

    lifecycle: WorkerLifecycle
    preflight: WorkerPreflight
    supervisor: WorkerSupervisor


def compose_worker_runtime(dependencies: WorkerDependencies) -> WorkerServices:
    """Construct the worker runtime without ambient clients or credentials."""
    lifecycle = WorkerLifecycle()
    preflight = WorkerPreflight(dependencies.platform_probe)
    supervisor = WorkerSupervisor(
        lifecycle,
        dependencies.startup_steps,
        dependencies.process_runner,
        ComfyHealthValidator(dependencies.comfy_api),
    )
    return WorkerServices(lifecycle, preflight, supervisor)
