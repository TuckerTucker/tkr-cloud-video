"""Publish-last idempotent result commit marker service."""

from __future__ import annotations

from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.delivery.contracts import ResultManifest
from tkr_cloud_video.delivery.uploader import ResultStore


class ResultCommitter:
    """Makes completion visible only through a final verified result.json object."""

    def __init__(self, store: ResultStore) -> None:
        """Initialize with the private output store."""
        self._store = store

    async def commit(self, manifest: ResultManifest) -> bool:
        """Publish a new marker or accept an exact idempotent retry."""
        content = manifest.canonical_bytes()
        # Existence is decided by `head`, never by a read: an absent object
        # whose virtual parent exists reads as empty content with a success
        # status, and empty content would otherwise present as a conflict.
        if await self._store.head(manifest.commit_key) is not None:
            existing = await self._store.get(manifest.commit_key)
            if existing != content:
                raise AppError(
                    "commit_conflict", "A different result is already committed."
                )
            return False
        for artifact in manifest.artifacts:
            metadata = await self._store.head(artifact.remote_key)
            if (
                metadata is None
                or metadata.size_bytes != artifact.size_bytes
                or metadata.sha256 != artifact.sha256
                or metadata.version_id != artifact.provider_version_id
            ):
                raise AppError(
                    "commit_evidence_invalid", "Result artifact evidence is incomplete."
                )
        metadata = await self._store.put(
            manifest.commit_key, content, str(manifest.digest())
        )
        if metadata.size_bytes != len(content) or metadata.sha256 != str(
            manifest.digest()
        ):
            raise AppError(
                "commit_verification_failed", "Result marker verification failed."
            )
        return True
