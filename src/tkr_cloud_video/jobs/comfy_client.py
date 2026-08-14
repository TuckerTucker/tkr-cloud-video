"""Typed ComfyUI prompt correlation port."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


class PromptState(StrEnum):
    """Observed ComfyUI prompt states."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class PromptStatus:
    """Safe correlated status and prompt-owned output records."""

    state: PromptState
    outputs: tuple[str, ...] = ()
    error_node: str | None = None
    # The provider's exception class name. Bounded by construction, unlike
    # its message and traceback, which are left where they are.
    error_type: str | None = None


class ComfyExecutionClient(Protocol):
    """Local authenticated ComfyUI execution adapter."""

    async def submit(self, workflow: dict[str, Any], client_id: str) -> str:
        """Submit a bound graph and return its prompt identity."""
        ...

    async def status(self, prompt_id: str) -> PromptStatus:
        """Return status for exactly one prompt."""
        ...

    async def cancel(self, prompt_id: str) -> None:
        """Cancel exactly one prompt."""
        ...
