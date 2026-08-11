"""Shell-free bounded subprocess execution adapters."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from tkr_cloud_video.core.errors import AppError


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Bounded process output owned by an adapter."""

    stdout: bytes
    stderr: bytes


class CommandExecutor(Protocol):
    """Asynchronous shell-free command execution port."""

    async def run(
        self,
        arguments: tuple[str, ...],
        environment: dict[str, str],
        *,
        stdin: bytes | None = None,
        timeout_seconds: float = 300,
    ) -> CommandResult:
        """Run exact arguments with a caller-constructed environment."""
        ...


class SubprocessCommandExecutor(CommandExecutor):
    """Run fixed executables without a shell or inherited environment."""

    def __init__(self, maximum_output_bytes: int = 8 * 1024 * 1024) -> None:
        """Initialize with a defensive captured-output bound."""
        if maximum_output_bytes < 1:
            raise ValueError("maximum_output_bytes must be positive")
        self._maximum_output_bytes = maximum_output_bytes

    async def run(
        self,
        arguments: tuple[str, ...],
        environment: dict[str, str],
        *,
        stdin: bytes | None = None,
        timeout_seconds: float = 300,
    ) -> CommandResult:
        """Execute, bound, and classify one child process invocation."""
        if not arguments or not arguments[0].startswith("/"):
            raise ValueError("adapter executable must be an absolute path")
        process = await asyncio.create_subprocess_exec(
            *arguments,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(stdin), timeout=timeout_seconds
            )
        except TimeoutError as error:
            process.kill()
            await process.wait()
            raise AppError(
                "adapter_timeout",
                "Provider adapter command exceeded its deadline.",
                retryable=True,
                context={"operation": "execute_adapter"},
                cause=error,
            ) from error
        if (
            len(stdout) > self._maximum_output_bytes
            or len(stderr) > self._maximum_output_bytes
        ):
            raise AppError(
                "adapter_output_exceeded",
                "Provider adapter output exceeded its safety bound.",
                context={"operation": "execute_adapter"},
            )
        if process.returncode != 0:
            raise AppError(
                "adapter_command_failed",
                "Provider adapter command failed.",
                retryable=True,
                context={"operation": "execute_adapter"},
                cause=RuntimeError(stderr.decode(errors="replace")),
            )
        return CommandResult(stdout, stderr)
