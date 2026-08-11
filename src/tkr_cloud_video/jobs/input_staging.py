"""Authorized, bounded, verified input staging."""

from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.jobs.contracts import InputReference
from tkr_cloud_video.security.validation import ObjectKey


class InputStagingError(AppError):
    """Private job input failed authorization or validation."""


class InputSource(Protocol):
    """Principal-aware private input adapter."""

    async def authorized(self, principal_id: str, key: ObjectKey) -> bool:
        """Return whether the principal owns or may use the private object."""
        ...

    async def download(self, key: ObjectKey, destination: Path, max_bytes: int) -> int:
        """Stream at most max_bytes into a caller-owned partial path."""
        ...


class InputMediaInspector(Protocol):
    """Media validation adapter for staged image/video inputs."""

    def inspect(self, path: Path) -> tuple[str, int, int]:
        """Return media type, width, and height."""
        ...


@dataclass(frozen=True, slots=True)
class StagedInput:
    """Verified normalized input supplied to workflow binding."""

    path: Path
    media_type: str
    width: int
    height: int
    size_bytes: int
    sha256: str


class InputStager:
    """Stages authorized inputs atomically inside a server-owned directory."""

    def __init__(
        self, source: InputSource, inspector: InputMediaInspector, max_bytes: int
    ) -> None:
        """Initialize with explicit adapters and bounded input policy."""
        self._source, self._inspector, self._max_bytes = source, inspector, max_bytes

    async def stage(
        self, principal_id: str, reference: InputReference, input_root: Path, index: int
    ) -> StagedInput:
        """Authorize, download, validate, and atomically promote one input."""
        key = ObjectKey(reference.object_key)
        if not await self._source.authorized(principal_id, key):
            raise InputStagingError(
                "input_unauthorized", "Private input is unauthorized."
            )
        partial = input_root / f".{uuid.uuid4().hex}.part"
        final = input_root / f"input-{index}.bin"
        try:
            reported = await self._source.download(key, partial, self._max_bytes)
            actual = partial.stat().st_size
            if reported != actual or actual > self._max_bytes:
                raise InputStagingError(
                    "input_size_invalid", "Private input exceeds policy."
                )
            digest = hashlib.sha256(partial.read_bytes()).hexdigest()
            if reference.sha256 is not None and digest != reference.sha256:
                raise InputStagingError(
                    "input_digest_mismatch", "Private input digest differs."
                )
            media_type, width, height = self._inspector.inspect(partial)
            if (
                media_type not in {"image/jpeg", "image/png", "video/mp4"}
                or width < 1
                or height < 1
            ):
                raise InputStagingError(
                    "input_media_invalid", "Private input media is invalid."
                )
            os.replace(partial, final)
            return StagedInput(final, media_type, width, height, actual, digest)
        finally:
            partial.unlink(missing_ok=True)
