"""Bounded ComfyUI prompt execution supervision."""

from __future__ import annotations

from typing import Any, Protocol

from tkr_cloud_video.core.clock import Clock
from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.jobs.comfy_client import (
    ComfyExecutionClient,
    PromptState,
    PromptStatus,
)


class Waiter(Protocol):
    """Injected asynchronous polling delay."""

    async def wait(self, seconds: float) -> None:
        """Wait without blocking the event loop."""
        ...


class JobExecutionError(AppError):
    """ComfyUI execution reached a typed unsuccessful outcome."""


class PromptExecutor:
    """Tracks one prompt through a bounded terminal outcome."""

    def __init__(
        self, client: ComfyExecutionClient, clock: Clock, waiter: Waiter
    ) -> None:
        """Initialize with explicit client, clock, and delay ports."""
        self._client, self._clock, self._waiter = client, clock, waiter

    async def execute(
        self, workflow: dict[str, Any], client_id: str, timeout_seconds: float
    ) -> PromptStatus:
        """Submit, poll, cancel on timeout, and classify terminal state."""
        prompt_id = await self._client.submit(workflow, client_id)
        deadline = self._clock.monotonic() + timeout_seconds
        while self._clock.monotonic() < deadline:
            status = await self._client.status(prompt_id)
            if status.state is PromptState.SUCCEEDED:
                return status
            if status.state is PromptState.FAILED:
                raise JobExecutionError(
                    "comfy_node_failed",
                    "ComfyUI reported a node execution failure.",
                    context={"resource_id": status.error_node or "unknown-node"},
                )
            if status.state is PromptState.CANCELLED:
                raise JobExecutionError(
                    "job_cancelled", "ComfyUI prompt was cancelled."
                )
            await self._waiter.wait(0.25)
        await self._client.cancel(prompt_id)
        raise JobExecutionError(
            "job_timeout", "ComfyUI prompt exceeded its deadline.", retryable=True
        )
