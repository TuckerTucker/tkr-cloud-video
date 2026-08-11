"""Publish-last immutable artifact catalog service."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from tkr_cloud_video.artifacts.models import ArtifactEntry, ModelSetManifest
from tkr_cloud_video.core.errors import AppError


class ArtifactPublicationError(AppError):
    """Artifact publication could not prove immutable remote content."""


@dataclass(frozen=True, slots=True)
class ObjectMetadata:
    """Provider-neutral remote object verification metadata."""

    size_bytes: int
    sha256: str
    version_id: str


class ArtifactStore(Protocol):
    """Private object-store port for immutable catalog operations."""

    async def head(self, key: str) -> ObjectMetadata | None:
        """Return object metadata or None when absent."""
        ...

    async def put(self, key: str, content: bytes, sha256: str) -> ObjectMetadata:
        """Create an object without overwriting an existing immutable key."""
        ...

    async def get(self, key: str) -> bytes:
        """Read exact private object bytes."""
        ...


@dataclass(frozen=True, slots=True)
class PublicationReceipt:
    """Safe immutable identity emitted after publish-last verification."""

    manifest_key: str
    manifest_digest: str
    manifest_version_id: str
    reused_blobs: int
    uploaded_blobs: int


class ArtifactPublisher:
    """Publishes verified blobs first and the digest-addressed manifest last."""

    def __init__(self, store: ArtifactStore) -> None:
        """Initialize with an injected private object store."""
        self._store = store

    async def publish_blob(self, source: Path, entry: ArtifactEntry) -> bool:
        """Verify a local source, then reuse or create its immutable remote key."""
        content = source.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if len(content) != entry.size_bytes or digest != entry.sha256:
            raise ArtifactPublicationError(
                "local_artifact_mismatch",
                "Local artifact does not match its manifest entry.",
                context={"resource_id": entry.name},
            )
        current = await self._store.head(entry.object_key)
        if current is not None:
            if current.size_bytes != entry.size_bytes or current.sha256 != entry.sha256:
                raise ArtifactPublicationError(
                    "immutable_blob_conflict",
                    "Content-addressed object exists with conflicting metadata.",
                    context={"resource_id": entry.name},
                )
            return True
        created = await self._store.put(entry.object_key, content, entry.sha256)
        if created.size_bytes != entry.size_bytes or created.sha256 != entry.sha256:
            raise ArtifactPublicationError(
                "remote_verification_failed",
                "Uploaded artifact failed remote verification.",
                context={"resource_id": entry.name},
            )
        return False

    async def publish(
        self, manifest: ModelSetManifest, sources: dict[str, Path]
    ) -> PublicationReceipt:
        """Publish every verified blob, then publish the immutable manifest last."""
        reused = 0
        uploaded = 0
        for entry in manifest.artifacts:
            source = sources.get(entry.name)
            if source is None:
                raise ArtifactPublicationError(
                    "artifact_source_missing",
                    "A manifest artifact has no local publication source.",
                    context={"resource_id": entry.name},
                )
            if await self.publish_blob(source, entry):
                reused += 1
            else:
                uploaded += 1
        return await self.publish_manifest(manifest, reused, uploaded)

    async def publish_manifest(
        self, manifest: ModelSetManifest, reused: int, uploaded: int
    ) -> PublicationReceipt:
        """Publish the commit-like manifest after every blob is verified."""
        if reused < 0 or uploaded < 0:
            raise ValueError("publication counts cannot be negative")
        content = manifest.canonical_bytes()
        digest = str(manifest.digest())
        key = f"manifests/sha256/{digest}.json"
        current = await self._store.head(key)
        metadata = current or await self._store.put(key, content, digest)
        if metadata.size_bytes != len(content) or metadata.sha256 != digest:
            raise ArtifactPublicationError(
                "manifest_verification_failed",
                "Published manifest failed remote verification.",
                context={"resource_id": manifest.model_set_id},
            )
        return PublicationReceipt(key, digest, metadata.version_id, reused, uploaded)
