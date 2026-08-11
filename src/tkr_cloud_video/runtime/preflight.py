"""Fail-closed GPU, disk, directory, and connectivity preflight."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.runtime.config import WorkerSettings


class PreflightError(AppError):
    """Worker prerequisites are not proven."""


class PlatformProbe(Protocol):
    """Injected platform capacity and endpoint checks."""

    def gpu_available(self) -> bool:
        """Return whether supported acceleration is visible."""
        ...

    def available_disk_bytes(self) -> int:
        """Return available worker-local bytes."""
        ...

    async def storage_authorized(self) -> bool:
        """Probe only the required private storage operation."""
        ...


@dataclass(frozen=True, slots=True)
class PreflightResult:
    """Safe proven worker capacity evidence."""

    available_disk_bytes: int
    required_disk_bytes: int
    gpu_available: bool
    storage_authorized: bool


class WorkerPreflight:
    """Validates all prerequisites before hydration begins."""

    def __init__(self, probe: PlatformProbe) -> None:
        """Initialize with an injected platform probe."""
        self._probe = probe

    async def run(
        self, settings: WorkerSettings, artifact_bytes: int
    ) -> PreflightResult:
        """Fail closed on GPU, directories, capacity, or authorized connectivity."""
        roots = (settings.cache_root, settings.model_root, settings.workspace_root)
        if any(not root.is_dir() for root in roots):
            raise PreflightError("directory_missing", "A worker root is unavailable.")
        gpu = self._probe.gpu_available()
        available = self._probe.available_disk_bytes()
        required = artifact_bytes * 2 + settings.disk_safety_bytes
        authorized = await self._probe.storage_authorized()
        if not gpu or available < required or not authorized:
            raise PreflightError(
                "preflight_failed", "Worker prerequisites are unavailable."
            )
        return PreflightResult(available, required, gpu, authorized)
