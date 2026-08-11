"""ComfyUI health and pinned workflow inventory validation."""

from __future__ import annotations

from typing import Protocol

from tkr_cloud_video.core.errors import AppError


class ComfyApi(Protocol):
    """Authenticated local-only ComfyUI API adapter."""

    async def healthy(self) -> bool:
        """Return whether local ComfyUI is healthy."""
        ...

    async def node_types(self) -> frozenset[str]:
        """Return installed node type names."""
        ...

    async def model_names(self) -> frozenset[str]:
        """Return available model names."""
        ...


class ComfyHealthValidator:
    """Proves health and exact workflow dependencies before readiness."""

    def __init__(self, api: ComfyApi) -> None:
        """Initialize with an authenticated local ComfyUI adapter."""
        self._api = api

    async def validate(
        self, required_nodes: frozenset[str], required_models: frozenset[str]
    ) -> None:
        """Fail readiness if health, nodes, or models are not proven."""
        if not await self._api.healthy():
            raise AppError("comfy_unhealthy", "ComfyUI health check failed.")
        if not required_nodes.issubset(await self._api.node_types()):
            raise AppError("comfy_node_missing", "A required ComfyUI node is missing.")
        if not required_models.issubset(await self._api.model_names()):
            raise AppError("comfy_model_missing", "A required model is missing.")
