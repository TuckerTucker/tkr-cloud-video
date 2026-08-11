"""Verified upload and publish-last commit tests."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.delivery.commit import ResultCommitter
from tkr_cloud_video.delivery.contracts import (
    RemoteArtifact,
    ResultManifest,
    ResultRole,
)
from tkr_cloud_video.delivery.uploader import (
    LocalArtifact,
    RemoteMetadata,
    ResultStore,
    VerifiedUploader,
)


@dataclass
class MemoryResultStore(ResultStore):
    """Immutable in-memory result store with write-order evidence."""

    objects: dict[str, bytes] = field(default_factory=dict)
    metadata: dict[str, RemoteMetadata] = field(default_factory=dict)
    writes: list[str] = field(default_factory=list)

    async def head(self, key: str) -> RemoteMetadata | None:
        """Return exact metadata."""
        return self.metadata.get(key)

    async def put(self, key: str, content: bytes, sha256: str) -> RemoteMetadata:
        """Create one immutable object."""
        assert key not in self.objects
        metadata = RemoteMetadata(
            len(content), sha256, f"version-{len(self.writes) + 1}"
        )
        self.objects[key], self.metadata[key] = content, metadata
        self.writes.append(key)
        return metadata

    async def get(self, key: str) -> bytes | None:
        """Return exact bytes when present."""
        return self.objects.get(key)


def local_artifacts(tmp_path: Path) -> tuple[LocalArtifact, ...]:
    """Create the complete exact local result set."""
    results: list[LocalArtifact] = []
    for role in ResultRole:
        content = f"{role.value}-content".encode()
        path = tmp_path / f"{role.value}.bin"
        path.write_bytes(content)
        results.append(
            LocalArtifact(role, path, hashlib.sha256(content).hexdigest(), len(content))
        )
    return tuple(results)


def manifest(evidence: tuple[RemoteArtifact, ...]) -> ResultManifest:
    """Build the final canonical marker from verified evidence."""
    return ResultManifest(
        job_id="job-1",
        attempt_id="attempt-1",
        workflow_digest="a" * 64,
        model_set_id="models-1",
        request_hash="b" * 64,
        committed_at=datetime(2026, 1, 1, tzinfo=UTC),
        artifacts=evidence,
    )


@pytest.mark.asyncio
async def test_commit_marker_is_written_last_and_retry_is_idempotent(
    tmp_path: Path,
) -> None:
    """Consumers cannot observe completion until all artifacts verify."""
    store = MemoryResultStore()
    evidence = await VerifiedUploader(store).upload(
        "job-1", "attempt-1", local_artifacts(tmp_path)
    )
    result = manifest(evidence)
    assert await ResultCommitter(store).commit(result) is True
    assert store.writes[-1] == result.commit_key
    assert await ResultCommitter(store).commit(result) is False


@pytest.mark.asyncio
async def test_missing_or_mismatched_remote_artifact_prevents_commit(
    tmp_path: Path,
) -> None:
    """A matching metadata set cannot hide an absent or changed video."""
    store = MemoryResultStore()
    evidence = await VerifiedUploader(store).upload(
        "job-1", "attempt-1", local_artifacts(tmp_path)
    )
    del store.metadata[evidence[0].remote_key]
    with pytest.raises(AppError):
        await ResultCommitter(store).commit(manifest(evidence))
    assert not any(key.endswith("result.json") for key in store.objects)


@pytest.mark.asyncio
async def test_uploader_rejects_incomplete_or_drifting_local_set(
    tmp_path: Path,
) -> None:
    """Only the exact validated three-role set reaches remote storage."""
    artifacts = local_artifacts(tmp_path)
    with pytest.raises(AppError):
        await VerifiedUploader(MemoryResultStore()).upload(
            "job-1", "attempt-1", artifacts[:-1]
        )
    artifacts[0].path.write_bytes(b"changed")
    with pytest.raises(AppError):
        await VerifiedUploader(MemoryResultStore()).upload(
            "job-1", "attempt-1", artifacts
        )


@pytest.mark.asyncio
async def test_conflicting_existing_marker_is_terminal(tmp_path: Path) -> None:
    """A committed attempt cannot be reopened with divergent canonical bytes."""
    store = MemoryResultStore()
    evidence = await VerifiedUploader(store).upload(
        "job-1", "attempt-1", local_artifacts(tmp_path)
    )
    result = manifest(evidence)
    await ResultCommitter(store).commit(result)
    store.objects[result.commit_key] = b"different"
    with pytest.raises(AppError) as captured:
        await ResultCommitter(store).commit(result)
    assert captured.value.code == "commit_conflict"
