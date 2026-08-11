"""Concrete local worker platform, process, clock, and secret adapters."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.security.process_secrets import SecretReference


class EnvironmentSecretResolver:
    """Resolve only explicitly named RunPod-injected environment secrets."""

    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        """Copy the caller's environment to prevent later mutation races."""
        self._environment = dict(environment if environment is not None else os.environ)

    def resolve(self, reference: SecretReference) -> str:
        """Return one non-empty secret value without logging or persistence."""
        value = self._environment.get(reference.secret_name)
        if value is None or not value:
            raise AppError(
                "secret_unavailable",
                "Required runtime secret is unavailable.",
                context={"resource_id": reference.secret_name},
            )
        return value


class AsyncioWaiter:
    """Non-blocking production polling delay."""

    async def wait(self, seconds: float) -> None:
        """Suspend the current task for a non-negative interval."""
        if seconds < 0:
            raise ValueError("wait duration cannot be negative")
        await asyncio.sleep(seconds)


@dataclass(frozen=True, slots=True)
class LocalPlatformProbe:
    """Probe GPU, local disk, and only the injected storage namespace."""

    cache_root: Path
    storage_client: object
    nvidia_smi: str = "/usr/bin/nvidia-smi"

    def gpu_available(self) -> bool:
        """Return whether NVIDIA's fixed probe sees at least one GPU."""
        try:
            result = subprocess.run(  # noqa: S603 - fixed executable and argument.
                (self.nvidia_smi, "-L"),
                check=False,
                capture_output=True,
                timeout=10,
                env={"PATH": "/usr/bin:/bin"},
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0 and bool(result.stdout.strip())

    def available_disk_bytes(self) -> int:
        """Return free bytes for the exact configured cache filesystem."""
        return shutil.disk_usage(self.cache_root).free

    async def storage_authorized(self) -> bool:
        """Probe the injected model-reader client without broad operations."""
        probe = getattr(self.storage_client, "probe", None)
        if probe is None:
            raise TypeError("storage client does not expose probe")
        result = await probe()
        return bool(result)


class ComfyProcessHandle:
    """Own graceful termination and bounded kill of one ComfyUI child."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        """Initialize with the exact child owned by the supervisor."""
        self._process = process
        streams = (process.stdout, process.stderr)
        self._drainers = tuple(
            asyncio.create_task(_drain_provider_output(stream))
            for stream in streams
            if stream is not None
        )

    async def terminate(self, timeout_seconds: float) -> None:
        """Terminate, then kill only this child if its bound expires."""
        if self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout_seconds)
            except TimeoutError:
                self._process.kill()
                await self._process.wait()
        if self._drainers:
            await asyncio.gather(*self._drainers)


@dataclass(frozen=True, slots=True)
class ComfyProcessRunner:
    """Launch a pinned local-only ComfyUI process with no storage secrets."""

    python_executable: str
    comfy_main: Path
    input_root: Path
    output_root: Path
    port: int = 8188
    user_root: Path | None = None
    temp_root: Path | None = None

    def __post_init__(self) -> None:
        """Require absolute fixed executables and a non-privileged port."""
        if (
            not self.python_executable.startswith("/")
            or not self.comfy_main.is_absolute()
            or (self.user_root is not None and not self.user_root.is_absolute())
            or (self.temp_root is not None and not self.temp_root.is_absolute())
        ):
            raise ValueError("ComfyUI executable paths must be absolute")
        if not 1024 <= self.port <= 65535:
            raise ValueError("ComfyUI port is invalid")

    async def start(self, environment: dict[str, str]) -> ComfyProcessHandle:
        """Start ComfyUI on loopback after rejecting secret-bearing variables."""
        forbidden = ("KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL")
        if any(any(part in name.upper() for part in forbidden) for name in environment):
            raise AppError(
                "comfy_secret_exposure",
                "ComfyUI environment contains a forbidden secret field.",
            )
        self.output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.input_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        user_root = self.user_root or self.input_root / ".comfy-user"
        temp_root = self.temp_root or self.input_root / ".comfy-temp"
        user_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        process = await asyncio.create_subprocess_exec(
            self.python_executable,
            str(self.comfy_main),
            "--listen",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--input-directory",
            str(self.input_root),
            "--output-directory",
            str(self.output_root),
            "--user-directory",
            str(user_root),
            "--temp-directory",
            str(temp_root),
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return ComfyProcessHandle(process)


async def _drain_provider_output(stream: asyncio.StreamReader) -> None:
    """Consume provider output without retaining potentially sensitive payloads."""
    while await stream.read(64 * 1024):
        continue
