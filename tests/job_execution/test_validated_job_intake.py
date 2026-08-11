"""Strict request, identity, workspace, and input staging tests."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import ValidationError

from tkr_cloud_video.jobs.contracts import InputReference, parse_generation_request
from tkr_cloud_video.jobs.identity import IdempotencyConflictError, IdempotencyRegistry
from tkr_cloud_video.jobs.input_staging import (
    InputMediaInspector,
    InputSource,
    InputStager,
    InputStagingError,
)
from tkr_cloud_video.jobs.workspace import WorkspaceManager
from tkr_cloud_video.security.validation import ObjectKey


def request_payload(mode: str = "text-to-video") -> dict[str, object]:
    """Build a minimally valid generation payload."""
    payload: dict[str, object] = {
        "mode": mode,
        "workflow_id": "h3-t2v-1",
        "model_set_id": "h3-models-1",
        "prompt": "A safe synthetic prompt",
        "seed": 42,
    }
    if mode == "image-to-video":
        payload["image"] = {"object_key": "inputs/principal/image.png"}
    if mode == "reference-to-video":
        payload["references"] = [{"object_key": "inputs/principal/reference.png"}]
    return payload


@pytest.mark.parametrize(
    "mode", ["text-to-video", "image-to-video", "reference-to-video"]
)
def test_request_modes_are_strict_and_canonical(mode: str) -> None:
    """Every supported mode parses and hashes deterministically."""
    first = parse_generation_request(request_payload(mode))
    second = parse_generation_request(request_payload(mode))
    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.request_hash() == second.request_hash()


@pytest.mark.parametrize(
    "change",
    [
        {"unknown": True},
        {"width": 257},
        {"frames": 0},
        {"prompt": ""},
        {"workflow_id": "../unsafe"},
    ],
)
def test_request_rejects_unknown_or_out_of_range_fields(
    change: dict[str, object],
) -> None:
    """Invalid requests fail before a job identity exists."""
    payload = {**request_payload(), **change}
    with pytest.raises(ValidationError):
        parse_generation_request(payload)


@pytest.mark.asyncio
async def test_concurrent_duplicates_converge_and_conflicts_fail() -> None:
    """One principal/key/payload tuple creates exactly one identity."""
    registry = IdempotencyRegistry()
    records = await asyncio.gather(
        *(registry.allocate("principal-1", "request-1", "a" * 64) for _ in range(8))
    )
    assert len({record.job_id for record in records}) == 1
    with pytest.raises(IdempotencyConflictError):
        await registry.allocate("principal-1", "request-1", "b" * 64)


@pytest.mark.asyncio
async def test_workspace_uses_server_identity_and_cleans_exact_attempt(
    tmp_path: Path,
) -> None:
    """Allocated paths are contained and cleanup does not touch sibling data."""
    registry = IdempotencyRegistry()
    identity = await registry.allocate("principal-1", "request-1", "a" * 64)
    sibling = tmp_path / "keep"
    sibling.write_text("keep")
    manager = WorkspaceManager(tmp_path)
    workspace = manager.allocate(identity)
    assert workspace.inputs.is_dir() and workspace.outputs.is_dir()
    assert identity.job_id in workspace.root.parts
    manager.cleanup(workspace)
    assert sibling.read_text() == "keep"


@dataclass
class MemoryInputSource(InputSource):
    """Authorized bounded private input fake."""

    content: bytes
    allow: bool = True

    async def authorized(self, principal_id: str, key: ObjectKey) -> bool:
        """Return configured authorization after checking scoped identity."""
        assert principal_id and str(key).startswith("inputs/")
        return self.allow

    async def download(self, key: ObjectKey, destination: Path, max_bytes: int) -> int:
        """Write only when content is within caller policy."""
        assert str(key) and len(self.content) <= max_bytes
        destination.write_bytes(self.content)
        return len(self.content)


class ImageInspector(InputMediaInspector):
    """Synthetic valid image inspector."""

    def inspect(self, path: Path) -> tuple[str, int, int]:
        """Return stable media evidence for non-empty bytes."""
        assert path.stat().st_size > 0
        return "image/png", 512, 512


@pytest.mark.asyncio
async def test_input_staging_authorizes_verifies_and_promotes(tmp_path: Path) -> None:
    """A valid private input becomes one normalized server-owned file."""
    content = b"synthetic-image"
    reference = InputReference(
        object_key="inputs/principal-1/image.png",
        sha256=hashlib.sha256(content).hexdigest(),
    )
    staged = await InputStager(MemoryInputSource(content), ImageInspector(), 100).stage(
        "principal-1", reference, tmp_path, 0
    )
    assert staged.path.name == "input-0.bin"
    assert staged.sha256 == reference.sha256
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.asyncio
async def test_input_staging_rejects_unauthorized_or_drifting_input(
    tmp_path: Path,
) -> None:
    """Authorization and digest failure leave no promoted or partial file."""
    reference = InputReference(
        object_key="inputs/principal-1/image.png", sha256="a" * 64
    )
    with pytest.raises(InputStagingError):
        await InputStager(
            MemoryInputSource(b"data", False), ImageInspector(), 100
        ).stage("principal-1", reference, tmp_path, 0)
    with pytest.raises(InputStagingError):
        await InputStager(MemoryInputSource(b"data"), ImageInspector(), 100).stage(
            "principal-1", reference, tmp_path, 0
        )
    assert list(tmp_path.iterdir()) == []
