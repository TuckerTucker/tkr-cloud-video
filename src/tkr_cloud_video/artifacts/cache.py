"""Content-addressed cache paths, verification, leases, and capacity policy."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from tkr_cloud_video.security.validation import Sha256Digest


@dataclass(frozen=True, slots=True)
class CachePolicy:
    """Bounded cache capacity and target high-water mark."""

    capacity_bytes: int
    high_water_bytes: int

    def __post_init__(self) -> None:
        """Require a positive high-water mark no larger than capacity."""
        if not 0 < self.high_water_bytes <= self.capacity_bytes:
            raise ValueError("cache high-water mark must be within capacity")


@dataclass
class CacheIndex:
    """Owns verified blob locations and in-memory active-reference leases."""

    root: Path
    policy: CachePolicy
    _leases: dict[str, int] = field(default_factory=dict, init=False)

    def initialize(self) -> None:
        """Create private blob and partial directories under the configured root."""
        self.blob_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.partial_root.mkdir(parents=True, exist_ok=True, mode=0o700)

    @property
    def blob_root(self) -> Path:
        """Return the server-owned verified blob root."""
        return self.root / "blobs" / "sha256"

    @property
    def partial_root(self) -> Path:
        """Return the disposable partial download root."""
        return self.root / "partials"

    def blob_path(self, digest: Sha256Digest) -> Path:
        """Return the content-addressed final path for a digest."""
        value = str(digest)
        return self.blob_root / value[:2] / value

    def verify(self, digest: Sha256Digest, expected_size: int) -> bool:
        """Stream-verify an existing cache blob for warm reuse."""
        path = self.blob_path(digest)
        if not path.is_file() or path.stat().st_size != expected_size:
            return False
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest() == str(digest)

    @staticmethod
    def verify_partial(path: Path, digest: Sha256Digest, expected_size: int) -> bool:
        """Verify a caller-owned partial without promoting it."""
        if not path.is_file() or path.stat().st_size != expected_size:
            return False
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest() == str(digest)

    @contextmanager
    def lease(self, digest: Sha256Digest) -> Iterator[Path]:
        """Protect a verified blob from eviction for a bounded operation."""
        key = str(digest)
        self._leases[key] = self._leases.get(key, 0) + 1
        try:
            yield self.blob_path(digest)
        finally:
            remaining = self._leases[key] - 1
            if remaining:
                self._leases[key] = remaining
            else:
                del self._leases[key]

    def is_leased(self, digest: Sha256Digest) -> bool:
        """Return whether a digest has active references."""
        return self._leases.get(str(digest), 0) > 0

    def size_bytes(self) -> int:
        """Return bytes used by whole verified blobs."""
        if not self.blob_root.exists():
            return 0
        return sum(
            path.stat().st_size for path in self.blob_root.glob("*/*") if path.is_file()
        )
