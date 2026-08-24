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
FORBIDDEN_STATUS: Final[int] = 403

# The delivery-read credential may read a committed object and nothing else: it
# carries no right to list the bucket. A provider that will not let a caller
# enumerate will not confirm or deny that a key exists either, so it answers a
# key that is merely not committed yet with 403 AccessDenied rather than 404.
#
# Read as a failure that is the whole wait for a generation, because the marker
# is absent for every poll until the moment it lands. The console would report
# an error for nine minutes and then, if it were still watching, a result. It is
# absence, and it is read as absence here.
#
# A credential that is wrong rather than narrow is refused under its own code —
# SignatureDoesNotMatch for a bad secret, InvalidAccessKeyId for a bad
# identifier — so a misconfigured console is still a failure and only this one
# code is forgiven.
ABSENT_ERROR_CODE: Final[str] = "AccessDenied"

# The refusal body is read only to classify the refusal, and a provider error
# document is a few hundred bytes.
MAX_ERROR_BYTES: Final[int] = 4096

# A committed marker is a few hundred bytes. The ceiling is here so a wrong or
# redirected key cannot stream an unbounded body into this process.
MAX_MARKER_BYTES: Final[int] = 1_048_576


def _provider_error_code(error: urllib.error.HTTPError) -> str | None:
    """Return the provider's own error code from a refusal body.

    The body is scanned rather than parsed. This process holds the delivery
    credential, so its dependency surface is part of its security posture, and
    an XML parser is more than classifying one short refusal is worth.

    Args:
        error: The refusal, whose body has not yet been read.

    Returns:
        The provider's error code, or None when the body carries none.

    """
    try:
        body = error.read(MAX_ERROR_BYTES).decode("utf-8", "replace")
    except OSError:
        return None
    opening = body.find("<Code>")
    if opening == -1:
        return None
    closing = body.find("</Code>", opening)
    if closing == -1:
        return None
    return body[opening + len("<Code>") : closing].strip()


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
            if (
                error.code == FORBIDDEN_STATUS
                and _provider_error_code(error) == ABSENT_ERROR_CODE
            ):
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
            The object's bytes, or None when the object is not there. A
            credential that may read a committed object but not list the bucket
            cannot tell absent from forbidden, and neither can this reader, so
            both answer None. That is the same collapse the delivery policy
            already makes deliberately in ``ResultAccessPolicy``.

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
