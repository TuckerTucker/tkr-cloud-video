"""Loopback-only ComfyUI JSON API integration adapter."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.jobs.comfy_client import PromptState, PromptStatus


class JsonTransport(Protocol):
    """Bounded asynchronous JSON transport port."""

    async def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> object:
        """Return one decoded JSON response."""
        ...


@dataclass(frozen=True, slots=True)
class UrllibJsonTransport(JsonTransport):
    """Dependency-free HTTP transport constrained to loopback ComfyUI."""

    base_url: str
    bearer_token: str | None = field(default=None, repr=False)
    timeout_seconds: float = 10
    maximum_response_bytes: int = 8 * 1024 * 1024

    def __post_init__(self) -> None:
        """Reject public endpoints and unsafe response bounds."""
        parsed = urlparse(self.base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("ComfyUI URL must use loopback HTTP")
        if self.timeout_seconds <= 0 or self.maximum_response_bytes < 1:
            raise ValueError("HTTP transport bounds must be positive")

    async def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> object:
        """Perform one request without blocking the event loop."""
        return await asyncio.to_thread(self._request_sync, method, path, payload)

    def _request_sync(
        self, method: str, path: str, payload: dict[str, Any] | None
    ) -> object:
        if not path.startswith("/") or ".." in PurePosixPath(path).parts:
            raise ValueError("HTTP path must be absolute and normalized")
        url = urljoin(self.base_url.rstrip("/") + "/", path.removeprefix("/"))
        body = (
            json.dumps(payload, separators=(",", ":")).encode()
            if payload is not None
            else None
        )
        headers = {"Content-Type": "application/json"}
        if self.bearer_token is not None:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        request = Request(  # noqa: S310 - base URL is validated as loopback HTTP.
            url, data=body, headers=headers, method=method
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
                content = response.read(self.maximum_response_bytes + 1)
        except (HTTPError, URLError, TimeoutError) as error:
            raise AppError(
                "comfy_request_failed",
                "ComfyUI API request failed.",
                retryable=True,
                context={"operation": "comfy_request"},
                cause=error,
            ) from error
        if len(content) > self.maximum_response_bytes:
            raise AppError(
                "comfy_response_exceeded",
                "ComfyUI API response exceeded its safety bound.",
                context={"operation": "comfy_request"},
            )
        try:
            return json.loads(content) if content else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AppError(
                "comfy_response_invalid",
                "ComfyUI API returned invalid JSON.",
                context={"operation": "comfy_request"},
                cause=error,
            ) from error


class ComfyApiClient:
    """Combined readiness and prompt adapter for one local ComfyUI process."""

    def __init__(self, transport: JsonTransport) -> None:
        """Initialize with an injected loopback JSON transport."""
        self._transport = transport

    async def healthy(self) -> bool:
        """Require a structurally valid system-stats response."""
        response = await self._transport.request("GET", "/system_stats")
        return isinstance(response, dict) and "system" in response

    async def node_types(self) -> frozenset[str]:
        """Return exact installed node type names."""
        response = await self._transport.request("GET", "/object_info")
        if not isinstance(response, dict):
            raise _invalid_response()
        return frozenset(str(name) for name in response)

    async def model_names(self) -> frozenset[str]:
        """Extract model file choices from the installed node inventory."""
        response = await self._transport.request("GET", "/object_info")
        if not isinstance(response, dict):
            raise _invalid_response()
        found: set[str] = set()
        _collect_model_names(response, found)
        return frozenset(found)

    async def submit(self, workflow: dict[str, Any], client_id: str) -> str:
        """Submit one bound workflow and return its provider prompt identity."""
        response = await self._transport.request(
            "POST", "/prompt", {"prompt": workflow, "client_id": client_id}
        )
        if not isinstance(response, dict) or not isinstance(
            response.get("prompt_id"), str
        ):
            raise _invalid_response()
        return str(response["prompt_id"])

    async def status(self, prompt_id: str) -> PromptStatus:
        """Classify only one prompt and its explicitly attributed outputs."""
        response = await self._transport.request("GET", f"/history/{prompt_id}")
        if not isinstance(response, dict):
            raise _invalid_response()
        record = response.get(prompt_id)
        if record is None:
            return PromptStatus(PromptState.RUNNING)
        if not isinstance(record, dict):
            raise _invalid_response()
        status = record.get("status", {})
        if not isinstance(status, dict):
            raise _invalid_response()
        status_text = status.get("status_str")
        if status_text in {"error", "failed"}:
            node, exception = _execution_error(status.get("messages"))
            return PromptStatus(
                PromptState.FAILED, error_node=node, error_type=exception
            )
        if status_text in {"cancelled", "canceled"}:
            return PromptStatus(PromptState.CANCELLED)
        completed = status.get("completed") is True or status_text == "success"
        if not completed:
            return PromptStatus(PromptState.RUNNING)
        return PromptStatus(PromptState.SUCCEEDED, _output_paths(record.get("outputs")))

    async def cancel(self, prompt_id: str) -> None:
        """Delete exactly one queued prompt; do not interrupt unrelated work."""
        await self._transport.request("POST", "/queue", {"delete": [prompt_id]})


class PollingComfyApi:
    """Bound ComfyUI startup retries while preserving the inventory contract."""

    def __init__(
        self,
        client: ComfyApiClient,
        timeout_seconds: float,
        poll_seconds: float = 0.25,
    ) -> None:
        """Initialize with explicit startup and polling bounds."""
        if timeout_seconds <= 0 or poll_seconds <= 0:
            raise ValueError("ComfyUI polling bounds must be positive")
        self._client = client
        self._timeout = timeout_seconds
        self._poll = poll_seconds

    async def healthy(self) -> bool:
        """Poll retryable connection failures until health or the deadline."""
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            try:
                if await self._client.healthy():
                    return True
            except AppError as error:
                if not error.retryable:
                    raise
            await asyncio.sleep(self._poll)
        return False

    async def node_types(self) -> frozenset[str]:
        """Delegate installed-node inventory after startup health passes."""
        return await self._client.node_types()

    async def model_names(self) -> frozenset[str]:
        """Delegate installed-model inventory after startup health passes."""
        return await self._client.model_names()


def _collect_model_names(value: object, found: set[str]) -> None:
    if isinstance(value, dict):
        for nested in value.values():
            _collect_model_names(nested, found)
    elif isinstance(value, list):
        for nested in value:
            _collect_model_names(nested, found)
    elif isinstance(value, str) and value.lower().endswith(
        (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf")
    ):
        found.add(value)


def _output_paths(value: object) -> tuple[str, ...]:
    if not isinstance(value, dict):
        return ()
    paths: list[str] = []
    for node_output in value.values():
        if not isinstance(node_output, dict):
            continue
        for collection_name in ("videos", "gifs", "images"):
            collection = node_output.get(collection_name, [])
            if not isinstance(collection, list):
                continue
            for item in collection:
                if not isinstance(item, dict) or item.get("type") != "output":
                    continue
                filename, subfolder = item.get("filename"), item.get("subfolder", "")
                if not isinstance(filename, str) or not isinstance(subfolder, str):
                    continue
                candidate = PurePosixPath(subfolder) / filename
                if candidate.is_absolute() or ".." in candidate.parts:
                    raise AppError(
                        "comfy_output_path_invalid",
                        "ComfyUI returned an unsafe output path.",
                    )
                paths.append(str(candidate))
    return tuple(paths)


def _execution_error(messages: object) -> tuple[str | None, str | None]:
    """Return the failing node and its exception class from provider messages.

    The provider also reports an exception message and a traceback. Both are
    free text that can carry a path or a payload, so only the class name is
    taken: it names the kind of failure without quoting anything.

    Args:
        messages: The provider's status message list.

    Returns:
        The node identifier and exception class name, each None when absent.

    """
    if not isinstance(messages, list):
        return None, None
    for item in messages:
        if not isinstance(item, list) or len(item) != 2 or item[0] != "execution_error":
            continue
        detail = item[1]
        if not isinstance(detail, dict):
            continue
        node = detail.get("node_id")
        exception = detail.get("exception_type")
        return (
            str(node) if isinstance(node, str) else None,
            str(exception) if isinstance(exception, str) else None,
        )
    return None, None


def _invalid_response() -> AppError:
    return AppError(
        "comfy_response_invalid",
        "ComfyUI API returned an invalid response.",
        context={"operation": "comfy_request"},
    )
