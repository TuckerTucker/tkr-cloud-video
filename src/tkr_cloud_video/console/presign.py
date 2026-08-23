"""Signature Version 4 query presigning for the reviewed Canadian B2 endpoint.

Presigning is the only way to hand a browser a private object without proxying
its bytes through this process, so it is implemented here rather than avoided.
It is deliberately narrow: one method, one immutable object, GET only, and a
lifetime the caller must state. Nothing here can create, overwrite, or delete.

The signature is computed from the standard library alone. That is a dependency
decision as much as a cryptographic one: the delivery credential is the most
sensitive thing this process holds, and hmac and hashlib are already in the
trusted base.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from typing import Final
from urllib.parse import quote, urlencode

from tkr_cloud_video.console.settings import ConsoleCredentials
from tkr_cloud_video.core.clock import Clock
from tkr_cloud_video.core.storage import validate_canadian_b2_endpoint
from tkr_cloud_video.security.validation import ObjectKey

ALGORITHM: Final[str] = "AWS4-HMAC-SHA256"
SERVICE: Final[str] = "s3"
TERMINATOR: Final[str] = "aws4_request"

# S3 signs a presigned GET against this literal rather than a body digest,
# which is what lets the URL be handed to a client that sends no body.
UNSIGNED_PAYLOAD: Final[str] = "UNSIGNED-PAYLOAD"

# The maximum SigV4 itself permits. The delivery policy's own one-hour cap is
# enforced by SignedLinkService, which sits above this; this bound only stops a
# caller from constructing a URL the provider would reject outright.
MAX_PRESIGN_SECONDS: Final[int] = 604_800


def _sign(key: bytes, message: str) -> bytes:
    """Return one HMAC-SHA256 round of the signing key derivation."""
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _encode_path_segment(value: str) -> str:
    """Percent-encode one path segment under S3's unreserved-character rule."""
    return quote(value, safe="-_.~")


class SigV4Presigner:
    """Issues read-only presigned URLs for one bucket in one reviewed region.

    The bucket, endpoint, and region are fixed at construction, so a key is the
    only thing a caller varies. A presigner built for the delivery bucket cannot
    be pointed at another bucket by a request.
    """

    def __init__(
        self,
        credentials: ConsoleCredentials,
        clock: Clock,
        *,
        bucket_name: str,
        endpoint: str,
        region: str,
    ) -> None:
        """Initialize with delivery-read credentials and a fixed scope.

        Args:
            credentials: The delivery-read credential pair.
            clock: Injected time source; the signature is dated from it.
            bucket_name: The one bucket this presigner may address.
            endpoint: The reviewed Canadian S3 endpoint.
            region: The region that endpoint's hostname must name.

        Raises:
            ValueError: The endpoint and region are not a reviewed pair.

        """
        validate_canadian_b2_endpoint(endpoint, region)
        self._credentials = credentials
        self._clock = clock
        self._bucket_name = bucket_name
        self._endpoint = endpoint.rstrip("/")
        self._region = region
        self._host = endpoint.split("://", 1)[1].rstrip("/")

    def presign_get(self, object_key: str, ttl_seconds: int) -> str:
        """Return a presigned GET URL for one exact immutable object.

        Args:
            object_key: The validated object key, relative to the bucket.
            ttl_seconds: Lifetime in seconds.

        Returns:
            The signed URL. It carries the credential's identifier and a
            signature, so it is a secret for its lifetime and must never be
            logged, recorded, or placed in an error context.

        Raises:
            ValueError: The lifetime is outside what a signature can express.

        """
        if not 1 <= ttl_seconds <= MAX_PRESIGN_SECONDS:
            raise ValueError("presigned lifetime is outside the signable range")
        key = str(ObjectKey(object_key))
        now = self._clock.now()
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")
        scope = f"{datestamp}/{self._region}/{SERVICE}/{TERMINATOR}"
        # Path-style addressing keeps the bucket inside the signed path rather
        # than in a hostname, so a key can never be read as a bucket.
        canonical_path = "/" + "/".join(
            _encode_path_segment(segment)
            for segment in (self._bucket_name, *key.split("/"))
        )
        query: Mapping[str, str] = {
            "X-Amz-Algorithm": ALGORITHM,
            "X-Amz-Credential": f"{self._credentials.delivery_key_id}/{scope}",
            "X-Amz-Date": amz_date,
            "X-Amz-Expires": str(ttl_seconds),
            "X-Amz-SignedHeaders": "host",
        }
        canonical_query = urlencode(sorted(query.items()), quote_via=quote, safe="-_.~")
        canonical_request = "\n".join(
            (
                "GET",
                canonical_path,
                canonical_query,
                f"host:{self._host}\n",
                "host",
                UNSIGNED_PAYLOAD,
            )
        )
        string_to_sign = "\n".join(
            (
                ALGORITHM,
                amz_date,
                scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            )
        )
        signing_key = _sign(
            f"AWS4{self._credentials.delivery_application_key}".encode(), datestamp
        )
        for part in (self._region, SERVICE, TERMINATOR):
            signing_key = _sign(signing_key, part)
        signature = hmac.new(
            signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return (
            f"{self._endpoint}{canonical_path}"
            f"?{canonical_query}&X-Amz-Signature={signature}"
        )

    async def sign(self, object_key: str, ttl_seconds: int) -> str:
        """Satisfy the delivery signing port.

        The computation is pure and local, so there is nothing to await; the
        port is asynchronous because a provider that issues links over its own
        API would need to be.
        """
        return self.presign_get(object_key, ttl_seconds)
