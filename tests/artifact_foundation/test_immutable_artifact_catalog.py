"""Immutable catalog publication and resolution tests."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from tkr_cloud_video.artifacts.models import (
    ArtifactEntry,
    ArtifactRole,
    ModelSetManifest,
)
from tkr_cloud_video.artifacts.publisher import (
    ArtifactPublicationError,
    ArtifactPublisher,
    ArtifactStore,
    ObjectMetadata,
)
from tkr_cloud_video.artifacts.resolver import ApprovedReleaseResolver
from tkr_cloud_video.security.validation import ObjectKey, Sha256Digest


@dataclass
class MemoryStore(ArtifactStore):
    """Versioned in-memory object-store contract fake."""

    objects: dict[str, bytes] = field(default_factory=dict)
    metadata: dict[str, ObjectMetadata] = field(default_factory=dict)
    writes: list[str] = field(default_factory=list)

    async def head(self, key: str) -> ObjectMetadata | None:
        """Return stored metadata."""
        return self.metadata.get(key)

    async def put(self, key: str, content: bytes, sha256: str) -> ObjectMetadata:
        """Store bytes once and return deterministic provider metadata."""
        assert key not in self.objects
        metadata = ObjectMetadata(
            len(content), sha256, f"version-{len(self.writes) + 1}"
        )
        self.objects[key] = content
        self.metadata[key] = metadata
        self.writes.append(key)
        return metadata

    async def get(self, key: str) -> bytes:
        """Return exact object bytes."""
        return self.objects[key]


def entry(name: str, role: ArtifactRole, content: bytes) -> ArtifactEntry:
    """Build an entry whose remote key is content addressed."""
    digest = hashlib.sha256(content).hexdigest()
    return ArtifactEntry(
        name=name,
        role=role,
        object_key=f"blobs/sha256/{digest}",
        sha256=digest,
        size_bytes=len(content),
        destination=f"{role.value}s/{name}.bin",
        dependencies=("model-1",) if role is ArtifactRole.WORKFLOW else (),
    )


def catalog() -> tuple[ModelSetManifest, dict[str, bytes]]:
    """Build a valid deterministic model set."""
    content = {"model-1": b"model-bytes", "workflow-1": b"workflow-bytes"}
    artifacts = (
        entry("model-1", ArtifactRole.MODEL, content["model-1"]),
        entry("workflow-1", ArtifactRole.WORKFLOW, content["workflow-1"]),
    )
    return (
        ModelSetManifest(
            model_set_id="minimax-h3-1",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            provenance_digest="a" * 64,
            license_approval_id="approval-1",
            artifacts=artifacts,
        ),
        content,
    )


def write_sources(tmp_path: Path, content: dict[str, bytes]) -> dict[str, Path]:
    """Create local publication source files."""
    sources: dict[str, Path] = {}
    for name, value in content.items():
        path = tmp_path / name
        path.write_bytes(value)
        sources[name] = path
    return sources


def test_manifest_is_canonical_and_referentially_complete() -> None:
    """Repeated manifest construction produces the same independent identity."""
    manifest, _ = catalog()
    repeated, _ = catalog()

    assert manifest.canonical_bytes() == repeated.canonical_bytes()
    assert manifest.digest() == repeated.digest()


def test_manifest_rejects_missing_dependency_and_mutable_key() -> None:
    """Incomplete workflow references and non-digest keys fail before publication."""
    manifest, _ = catalog()
    bad_workflow = manifest.artifacts[1].model_copy(
        update={"dependencies": ("missing-model",)}
    )
    with pytest.raises(ValidationError):
        ModelSetManifest.model_validate(
            {
                **manifest.model_dump(),
                "artifacts": [manifest.artifacts[0], bad_workflow],
            }
        )
    with pytest.raises(ValidationError):
        manifest.artifacts[0].model_copy(
            update={"object_key": "models/latest.bin"}
        ).model_validate(
            {**manifest.artifacts[0].model_dump(), "object_key": "models/latest.bin"}
        )


@pytest.mark.asyncio
async def test_publish_uploads_blobs_before_manifest_and_resolves_exact_bytes(
    tmp_path: Path,
) -> None:
    """The commit-like manifest is last and its pinned digest resolves safely."""
    manifest, content = catalog()
    store = MemoryStore()
    receipt = await ArtifactPublisher(store).publish(
        manifest, write_sources(tmp_path, content)
    )

    assert store.writes[-1] == receipt.manifest_key
    assert receipt.uploaded_blobs == 2
    resolved = await ApprovedReleaseResolver(store).resolve(
        ObjectKey(receipt.manifest_key), Sha256Digest(receipt.manifest_digest)
    )
    assert resolved == manifest


@pytest.mark.asyncio
async def test_publish_reuses_identical_blobs_and_rejects_conflict(
    tmp_path: Path,
) -> None:
    """Existing matching bytes are reused; conflicting metadata is never overwritten."""
    manifest, content = catalog()
    store = MemoryStore()
    sources = write_sources(tmp_path, content)
    publisher = ArtifactPublisher(store)
    await publisher.publish(manifest, sources)
    repeated = await publisher.publish(manifest, sources)
    assert repeated.reused_blobs == 2

    first = manifest.artifacts[0]
    store.metadata[first.object_key] = ObjectMetadata(
        first.size_bytes + 1, first.sha256, "x"
    )
    with pytest.raises(ArtifactPublicationError):
        await publisher.publish(manifest, sources)


@pytest.mark.asyncio
async def test_publish_rejects_local_drift_or_missing_source(tmp_path: Path) -> None:
    """Local size/digest mismatches and absent sources block all later publication."""
    manifest, content = catalog()
    sources = write_sources(tmp_path, content)
    sources["model-1"].write_bytes(b"changed")
    with pytest.raises(ArtifactPublicationError):
        await ArtifactPublisher(MemoryStore()).publish(manifest, sources)
    with pytest.raises(ArtifactPublicationError):
        await ArtifactPublisher(MemoryStore()).publish(manifest, {})


@pytest.mark.asyncio
async def test_resolver_rejects_mutated_manifest_or_missing_reference(
    tmp_path: Path,
) -> None:
    """Pinned substitution and referenced-object drift fail before hydration."""
    manifest, content = catalog()
    store = MemoryStore()
    receipt = await ArtifactPublisher(store).publish(
        manifest, write_sources(tmp_path, content)
    )
    store.objects[receipt.manifest_key] += b" "
    resolver = ApprovedReleaseResolver(store)
    with pytest.raises(ArtifactPublicationError):
        await resolver.resolve(
            ObjectKey(receipt.manifest_key), Sha256Digest(receipt.manifest_digest)
        )

    store.objects[receipt.manifest_key] = manifest.canonical_bytes()
    del store.metadata[manifest.artifacts[0].object_key]
    with pytest.raises(ArtifactPublicationError):
        await resolver.resolve(
            ObjectKey(receipt.manifest_key), Sha256Digest(receipt.manifest_digest)
        )
