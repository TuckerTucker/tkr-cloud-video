"""Worker configuration, preflight, lifecycle, health, and supervision tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.runtime.comfy_health import ComfyHealthValidator
from tkr_cloud_video.runtime.config import WorkerSettings
from tkr_cloud_video.runtime.health_api import health_response
from tkr_cloud_video.runtime.lifecycle import WorkerLifecycle, WorkerState
from tkr_cloud_video.runtime.preflight import PreflightError, WorkerPreflight
from tkr_cloud_video.runtime.supervisor import WorkerSupervisor
from tkr_cloud_video.worker_runtime.composition import (
    WorkerDependencies,
    compose_worker_runtime,
)


def settings(root: Path) -> WorkerSettings:
    """Build valid worker settings with existing local roots."""
    paths = [root / name for name in ("cache", "models", "work")]
    for path in paths:
        path.mkdir(parents=True)
    return WorkerSettings(
        release_id="release-1",
        worker_id="worker-1",
        model_set_id="model-1",
        manifest_digest="a" * 64,
        bucket_name="bucket-1",
        cache_root=paths[0],
        model_root=paths[1],
        workspace_root=paths[2],
        output_root=paths[2],
        comfyui_root=paths[2],
        disk_safety_bytes=10,
    )


@dataclass
class Probe:
    """Deterministic platform probe."""

    gpu: bool = True
    disk: int = 100
    authorized: bool = True

    def gpu_available(self) -> bool:
        return self.gpu

    def available_disk_bytes(self) -> int:
        return self.disk

    async def storage_authorized(self) -> bool:
        return self.authorized


@pytest.mark.asyncio
async def test_preflight_passes_and_fails_before_hydration(tmp_path: Path) -> None:
    """Capacity and authorization are explicit fail-closed gates."""
    result = await WorkerPreflight(Probe()).run(settings(tmp_path), 20)
    assert result.required_disk_bytes == 50
    with pytest.raises(PreflightError):
        await WorkerPreflight(Probe(disk=1)).run(settings(tmp_path / "second"), 20)


@dataclass
class Steps:
    """Ordered startup fake."""

    calls: list[str] = field(default_factory=list)

    async def configure(self) -> None:
        self.calls.append("configure")

    async def preflight(self) -> None:
        self.calls.append("preflight")

    async def hydrate(self) -> None:
        self.calls.append("hydrate")


@dataclass
class Handle:
    """Bounded process fake."""

    terminated: bool = False

    async def terminate(self, timeout_seconds: float) -> None:
        assert timeout_seconds > 0
        self.terminated = True


@dataclass
class Runner:
    """Process runner asserting hydration ordering."""

    steps: Steps
    handle: Handle = field(default_factory=Handle)

    async def start(self, environment: dict[str, str]) -> Handle:
        assert self.steps.calls[-1] == "hydrate"
        assert "SECRET" not in environment
        self.steps.calls.append("start")
        return self.handle


@dataclass
class Api:
    """Healthy exact ComfyUI inventory fake."""

    healthy_value: bool = True

    async def healthy(self) -> bool:
        return self.healthy_value

    async def node_types(self) -> frozenset[str]:
        return frozenset({"Node"})

    async def model_names(self) -> frozenset[str]:
        return frozenset({"Model"})


@pytest.mark.asyncio
async def test_supervisor_becomes_ready_only_after_validation_then_drains() -> None:
    """Startup order is exact and readiness drops before process termination."""
    lifecycle, steps = WorkerLifecycle(), Steps()
    runner = Runner(steps)
    supervisor = WorkerSupervisor(lifecycle, steps, runner, ComfyHealthValidator(Api()))
    await supervisor.start(
        {"MODEL_ROOT": "/models"}, frozenset({"Node"}), frozenset({"Model"})
    )
    assert lifecycle.ready and steps.calls == [
        "configure",
        "preflight",
        "hydrate",
        "start",
    ]
    assert health_response(lifecycle, "release-1", "probe", "probe")["ready"] is True
    await supervisor.drain(1)
    assert lifecycle.state is WorkerState.STOPPED and runner.handle.terminated


@pytest.mark.asyncio
async def test_health_failure_is_terminal_unready() -> None:
    """An unhealthy child never reaches admission."""
    lifecycle, steps = WorkerLifecycle(), Steps()
    supervisor = WorkerSupervisor(
        lifecycle, steps, Runner(steps), ComfyHealthValidator(Api(False))
    )
    with pytest.raises(AppError):
        await supervisor.start({}, frozenset(), frozenset())
    assert not lifecycle.ready and lifecycle.state is WorkerState.FAILED


def test_lifecycle_rejects_skipped_phase_and_health_auth() -> None:
    """Illegal transitions and unauthenticated probes fail safely."""
    lifecycle = WorkerLifecycle()
    with pytest.raises(AppError):
        lifecycle.transition(WorkerState.READY)
    with pytest.raises(AppError):
        health_response(lifecycle, "release-1", "bad", "good")


def test_composition_shares_one_lifecycle() -> None:
    """The composition root wires injected ports into a single state owner."""
    steps = Steps()
    services = compose_worker_runtime(
        WorkerDependencies(Probe(), Runner(steps), steps, Api())
    )
    assert services.supervisor.lifecycle is services.lifecycle
    assert services.preflight is not None
