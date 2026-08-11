"""Resolve independently pinned model-set manifests."""

from __future__ import annotations

import hashlib

from pydantic import ValidationError

from tkr_cloud_video.artifacts.models import ModelSetManifest
from tkr_cloud_video.artifacts.publisher import ArtifactPublicationError, ArtifactStore
from tkr_cloud_video.security.validation import ObjectKey, Sha256Digest


class ApprovedReleaseResolver:
    """Fetches and validates exact manifest bytes before hydration begins."""

    def __init__(self, store: ArtifactStore) -> None:
        """Initialize with an injected read-capable catalog store."""
        self._store = store

    async def resolve(self, key: ObjectKey, digest: Sha256Digest) -> ModelSetManifest:
        """Verify external identity, parse strictly, and check referenced metadata."""
        content = await self._store.get(str(key))
        if hashlib.sha256(content).hexdigest() != str(digest):
            raise ArtifactPublicationError(
                "manifest_identity_mismatch",
                "Resolved manifest does not match its pinned identity.",
                context={"operation": "resolve_manifest"},
            )
        try:
            manifest = ModelSetManifest.model_validate_json(content)
        except ValidationError as error:
            raise ArtifactPublicationError(
                "manifest_invalid",
                "Resolved manifest failed schema validation.",
                context={"operation": "resolve_manifest"},
                cause=error,
            ) from error
        for entry in manifest.artifacts:
            metadata = await self._store.head(entry.object_key)
            if (
                metadata is None
                or metadata.sha256 != entry.sha256
                or metadata.size_bytes != entry.size_bytes
            ):
                raise ArtifactPublicationError(
                    "manifest_reference_invalid",
                    "Manifest references an absent or drifting artifact.",
                    context={"resource_id": entry.name},
                )
        return manifest
