"""Read-only fetch of one private object over a short-lived presigned URL.

The console needs to read exactly one kind of object for itself: the result
marker that says a generation committed. It reads it the same way a browser
reads the video, through a presigned GET, so this process needs no second
credential mechanism and no provider client. The link it signs for its own read
lives for seconds and is used once.
"""

from __future__ import annotations

import asyncio
import urllib.error
import urllib.request
from typing import Final, Protocol

from tkr_cloud_video.console.presign import SigV4Presigner
from tkr_cloud_video.core.errors import AppError

NOT_FOUND_STATUS: Final[int] = 404
# A committed marker is a few hundred bytes. The ceiling is here so a wrong or
# redirected key cannot stream an unbounded body into this process.
MAX_MARKER_BYTES: Final[int] = 1_048_576


class ObjectReader(Protocol):
    """Read port for one exact private object."""

    async def get(self, object_key: str) -> bytes | None:
        """Return an object's bytes, or None when no such object exists."""
        ...


class PresignedObjectReader:
    """Fetches one exact private object, returning None only when absent."""

    def __init__(
        self,
        presigner: SigV4Presigner,
        *,
        ttl_seconds: int,
        timeout_seconds: float,
    ) -> None:
        """Initialize with the delivery presigner and its own read bounds.

        Args:
            presigner: The delivery-read presigner.
            ttl_seconds: Lifetime of the internal link signed per read.
            timeout_seconds: Deadline for the fetch.

        """
        self._presigner = presigner
        self._ttl_seconds = ttl_seconds
        self._timeout_seconds = timeout_seconds

    def _fetch(self, url: str) -> bytes | None:
        """Perform one bounded blocking GET of a presigned URL."""
        request = urllib.request.Request(url, method="GET")  # noqa: S310
        try:
            # The URL is constructed by this process from a validated https
            # endpoint and a validated key, never taken from a caller, so the
            # scheme cannot be file: or another local handler.
            with urllib.request.urlopen(  # noqa: S310
                request, timeout=self._timeout_seconds
            ) as response:
                return bytes(response.read(MAX_MARKER_BYTES + 1))
        except urllib.error.HTTPError as error:
            if error.code == NOT_FOUND_STATUS:
                return None
            raise AppError(
                "object_read_failed",
                "Reading a committed result marker failed.",
                retryable=error.code >= 500,
                context={"operation": "get_object"},
                cause=error,
            ) from error
        except OSError as error:
            raise AppError(
                "object_read_failed",
                "Reading a committed result marker failed.",
                retryable=True,
                context={"operation": "get_object"},
                cause=error,
            ) from error

    async def get(self, object_key: str) -> bytes | None:
        """Read one object by key.

        Args:
            object_key: The validated key, relative to the delivery bucket.

        Returns:
            The object's bytes, or None when no such object exists.

        Raises:
            AppError: The read failed, or the object exceeded the marker
                ceiling. The error names the operation and never the URL.

        """
        url = self._presigner.presign_get(object_key, self._ttl_seconds)
        content = await asyncio.to_thread(self._fetch, url)
        if content is not None and len(content) > MAX_MARKER_BYTES:
            raise AppError(
                "object_too_large",
                "A result marker exceeded the readable ceiling.",
                context={"operation": "get_object"},
            )
        return content
