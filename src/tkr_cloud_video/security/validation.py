"""Strict identifiers, remote keys, digests, and local path containment."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, TypeVar

from tkr_cloud_video.core.context import validate_identifier
from tkr_cloud_video.core.errors import AppError

DIGEST_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
T = TypeVar("T", bound="SafeIdentifier")


class BoundaryValidationError(AppError):
    """An external identifier, digest, or key is malformed."""


class PathContainmentError(AppError):
    """A local destination escapes its server-owned root."""


@dataclass(frozen=True, slots=True)
class SafeIdentifier:
    """Base value object for a validated opaque identifier."""

    value: str

    def __post_init__(self) -> None:
        """Validate with the shared safe identifier grammar."""
        try:
            validate_identifier(self.value, "identifier")
        except AppError as error:
            raise BoundaryValidationError(
                "invalid_boundary_value",
                "External identifier is invalid.",
                context={"field": type(self).__name__},
                cause=error,
            ) from error

    def __str__(self) -> str:
        """Return the validated opaque value."""
        return self.value


class JobId(SafeIdentifier):
    """Validated job identifier."""


class ModelSetId(SafeIdentifier):
    """Validated model-set identifier."""


@dataclass(frozen=True, slots=True)
class Sha256Digest:
    """Exactly 64 lowercase hexadecimal SHA-256 characters."""

    value: str

    def __post_init__(self) -> None:
        """Reject encodings, separators, case ambiguity, and wrong lengths."""
        if not DIGEST_PATTERN.fullmatch(self.value):
            raise BoundaryValidationError(
                "invalid_boundary_value",
                "SHA-256 digest must be 64 lowercase hexadecimal characters.",
                context={"field": "sha256"},
            )

    def __str__(self) -> str:
        """Return the normalized digest."""
        return self.value


def _validate_relative_posix(value: str, field: str) -> str:
    """Validate a normalized relative POSIX key without touching a filesystem."""
    if (
        not value
        or value.startswith("/")
        or value.endswith("/")
        or "\\" in value
        or "//" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise BoundaryValidationError(
            "invalid_boundary_value",
            "Path-like value is not a normalized relative POSIX path.",
            context={"field": field},
        )
    path = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in path.parts) or str(path) != value:
        raise BoundaryValidationError(
            "invalid_boundary_value",
            "Path-like value contains an ambiguous segment.",
            context={"field": field},
        )
    return value


@dataclass(frozen=True, slots=True)
class ObjectKey:
    """Validated object-store key, never interpreted as a local path."""

    value: str

    def __post_init__(self) -> None:
        """Reject traversal and ambiguous remote-key syntax."""
        _validate_relative_posix(self.value, "object_key")

    def __str__(self) -> str:
        """Return the validated remote key."""
        return self.value


@dataclass(frozen=True, slots=True)
class RelativeDestination:
    """Validated relative destination requiring later root containment."""

    value: str

    def __post_init__(self) -> None:
        """Reject traversal and ambiguous local-relative syntax."""
        _validate_relative_posix(self.value, "relative_destination")

    def __str__(self) -> str:
        """Return the validated relative destination."""
        return self.value


def contained_path(root: Path, destination: RelativeDestination) -> Path:
    """Resolve a destination beneath a canonical root, following existing symlinks.

    Args:
        root: Server-owned configured root that must already exist.
        destination: Validated relative destination.

    Returns:
        Canonical target path contained beneath the canonical root.

    Raises:
        PathContainmentError: If the root is invalid or the target escapes it.

    """
    canonical_root = root.resolve(strict=True)
    if not canonical_root.is_dir():
        raise PathContainmentError(
            "path_escape",
            "Assigned path root is not a directory.",
            context={"field": "root"},
        )
    lexical_target = canonical_root / destination.value
    target = lexical_target.parent.resolve(strict=False) / lexical_target.name
    try:
        target.relative_to(canonical_root)
    except ValueError as error:
        raise PathContainmentError(
            "path_escape",
            "Resolved path escapes its assigned root.",
            context={"field": "relative_destination"},
            cause=error,
        ) from error
    return target
