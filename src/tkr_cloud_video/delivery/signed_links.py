"""Short-lived private delivery link adapter boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from tkr_cloud_video.core.clock import Clock
from tkr_cloud_video.core.errors import AppError


class LinkSigner(Protocol):
    """Delivery-read-only provider signing port."""

    async def sign(self, object_key: str, ttl_seconds: int) -> str:
        """Return an opaque provider URL for one immutable object."""
        ...


@dataclass(frozen=True, slots=True)
class DownloadLink:
    """Ephemeral response value that must never enter structured logs."""

    url: str
    expires_at: datetime


class SignedLinkService:
    """Issues links under one configured maximum TTL."""

    def __init__(self, signer: LinkSigner, clock: Clock, max_ttl_seconds: int) -> None:
        """Initialize with delivery-only signing capability and injected time."""
        if not 1 <= max_ttl_seconds <= 3600:
            raise ValueError("signed-link TTL must be between 1 and 3600 seconds")
        self._signer, self._clock, self._max_ttl = signer, clock, max_ttl_seconds

    async def issue(self, object_key: str, requested_ttl_seconds: int) -> DownloadLink:
        """Issue one narrowly scoped link with a bounded positive TTL."""
        if requested_ttl_seconds < 1:
            raise AppError("link_ttl_invalid", "Download link TTL is invalid.")
        ttl = min(requested_ttl_seconds, self._max_ttl)
        url = await self._signer.sign(object_key, ttl)
        return DownloadLink(url, self._clock.now() + timedelta(seconds=ttl))
