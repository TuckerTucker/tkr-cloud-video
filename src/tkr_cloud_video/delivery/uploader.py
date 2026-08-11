"""Exact local result upload and remote verification."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.delivery.contracts import RemoteArtifact, ResultRole


@dataclass(frozen=True, slots=True)
class RemoteMetadata:
    """Provider-neutral immutable object evidence."""

    size_bytes: int
    sha256: str
    version_id: str


class ResultStore(Protocol):
    """Private output object-store port."""

    async def head(self, key: str) -> RemoteMetadata | None:
        """Return metadata for one exact object."""
        ...

    async def put(self, key: str, content: bytes, sha256: str) -> RemoteMetadata:
        """Create an immutable object without overwriting a conflicting key."""
        ...

    async def get(self, key: str) -> bytes | None:
        """Return exact bytes or None when absent."""
        ...


@dataclass(frozen=True, slots=True)
class LocalArtifact:
    """Validated local artifact selected explicitly by role."""

    role: ResultRole
    path: Path
    sha256: str
    size_bytes: int


class VerifiedUploader:
    """Uploads to one immutable attempt prefix and verifies each object."""

    def __init__(self, store: ResultStore) -> None:
        """Initialize with an output-write-only adapter."""
        self._store = store

    async def upload(
        self, job_id: str, attempt_id: str, artifacts: tuple[LocalArtifact, ...]
    ) -> tuple[RemoteArtifact, ...]:
        """Upload and remotely verify every exact required artifact."""
        if {artifact.role for artifact in artifacts} != set(ResultRole):
            raise AppError(
                "upload_set_incomplete", "Required result artifact set is incomplete."
            )
        evidence: list[RemoteArtifact] = []
        for artifact in artifacts:
            content = artifact.path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            if len(content) != artifact.size_bytes or digest != artifact.sha256:
                raise AppError(
                    "local_result_drift", "Local result artifact has changed."
                )
            key = f"outputs/{job_id}/{attempt_id}/{artifact.role.value}.bin"
            current = await self._store.head(key)
            metadata = current or await self._store.put(key, content, digest)
            if metadata.size_bytes != len(content) or metadata.sha256 != digest:
                raise AppError(
                    "remote_result_mismatch", "Remote result verification failed."
                )
            evidence.append(
                RemoteArtifact(
                    role=artifact.role,
                    remote_key=key,
                    size_bytes=len(content),
                    sha256=digest,
                    provider_version_id=metadata.version_id,
                )
            )
        return tuple(evidence)
