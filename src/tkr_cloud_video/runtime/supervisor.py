"""Hydration-gated ComfyUI process supervision and bounded drain."""

from __future__ import annotations

from typing import Protocol

from tkr_cloud_video.runtime.comfy_health import ComfyHealthValidator
from tkr_cloud_video.runtime.lifecycle import WorkerLifecycle, WorkerState


class ProcessHandle(Protocol):
    """Injected child process handle."""

    async def terminate(self, timeout_seconds: float) -> None:
        """Terminate within the supplied drain bound."""
        ...


class ProcessRunner(Protocol):
    """Injected allowlisted process launcher."""

    async def start(self, environment: dict[str, str]) -> ProcessHandle:
        """Start ComfyUI with an explicitly constructed environment."""
        ...


class StartupSteps(Protocol):
    """Injected preflight and hydration use cases."""

    async def configure(self) -> None:
        """Validate configuration without side effects."""
        ...

    async def preflight(self) -> None:
        """Prove capacity and connectivity."""
        ...

    async def hydrate(self) -> None:
        """Hydrate and materialize verified artifacts."""
        ...


class WorkerSupervisor:
    """Owns exact startup ordering, admission, and process termination."""

    def __init__(
        self,
        lifecycle: WorkerLifecycle,
        steps: StartupSteps,
        runner: ProcessRunner,
        health: ComfyHealthValidator,
    ) -> None:
        """Initialize with isolated lifecycle, startup, process, and health ports."""
        self.lifecycle = lifecycle
        self._steps = steps
        self._runner = runner
        self._health = health
        self._process: ProcessHandle | None = None

    async def start(
        self,
        environment: dict[str, str],
        required_nodes: frozenset[str] | None = None,
        required_models: frozenset[str] | None = None,
    ) -> None:
        """Start ComfyUI only after configuration, preflight, and hydration."""
        try:
            await self._steps.configure()
            self.lifecycle.transition(WorkerState.PREFLIGHTING)
            await self._steps.preflight()
            self.lifecycle.transition(WorkerState.HYDRATING)
            await self._steps.hydrate()
            if required_nodes is None or required_models is None:
                inventory = self._steps
                node_getter = getattr(inventory, "required_nodes", None)
                model_getter = getattr(inventory, "required_models", None)
                if node_getter is None or model_getter is None:
                    raise TypeError("startup steps do not expose hydrated inventory")
                required_nodes = frozenset(node_getter())
                required_models = frozenset(model_getter())
            self.lifecycle.transition(WorkerState.STARTING)
            self._process = await self._runner.start(environment)
            self.lifecycle.transition(WorkerState.VALIDATING)
            await self._health.validate(required_nodes, required_models)
            self.lifecycle.transition(WorkerState.READY)
        except Exception as error:
            self.lifecycle.fail(type(error).__name__)
            raise

    async def drain(self, timeout_seconds: float) -> None:
        """Drop readiness before bounded child termination."""
        if self.lifecycle.state is WorkerState.READY:
            self.lifecycle.transition(WorkerState.DRAINING)
        if self._process is not None:
            await self._process.terminate(timeout_seconds)
        if self.lifecycle.state in {WorkerState.DRAINING, WorkerState.FAILED}:
            self.lifecycle.transition(WorkerState.STOPPED)
