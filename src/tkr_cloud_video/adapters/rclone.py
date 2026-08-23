"""Backblaze B2 object storage through an isolated rclone process."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from tkr_cloud_video.adapters.process import CommandExecutor
from tkr_cloud_video.artifacts.publisher import ObjectMetadata
from tkr_cloud_video.core.context import validate_identifier
from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.core.storage import (
    B2_S3_ENDPOINT,
    B2_S3_REGION,
    validate_canadian_b2_endpoint,
)
from tkr_cloud_video.delivery.reconciler import StoredObject
from tkr_cloud_video.delivery.uploader import RemoteMetadata
from tkr_cloud_video.security.validation import ObjectKey, Sha256Digest

RCLONE_EXECUTABLE = "/opt/tkr-cloud-video/bin/rclone"

# Model blobs reach 21 GB and ADR-002 placed workers across six territories, so
# a bulk transfer's deadline has to bound an intercontinental pull rather than
# an API call. The executor's 300-second default deadlines the diffusion model
# below any throughput a long-haul link plausibly sustains.
TRANSFER_TIMEOUT_SECONDS = 14_400

# rclone splits a transfer above --multi-thread-cutoff across this many ranged
# readers. Its default of 4 suits a same-region link; a worker in AU, IN or JP
# reading from ca-east-006 needs more concurrency because round-trip time, not
# bandwidth, bounds what one stream achieves.
TRANSFER_STREAMS = 8


@dataclass(frozen=True, slots=True)
class RcloneCredentials:
    """In-memory B2 credential pair that is never rendered or persisted."""

    key_id: str = field(repr=False)
    application_key: str = field(repr=False)

    def __post_init__(self) -> None:
        """Reject empty secret values."""
        if not self.key_id or not self.application_key:
            raise ValueError("B2 credentials cannot be empty")


@dataclass(frozen=True, slots=True)
class RcloneLocation:
    """One validated remote, bucket, and credential-enforced base prefix."""

    remote_name: str
    bucket_name: str
    base_prefix: str
    endpoint: str = B2_S3_ENDPOINT
    region: str = B2_S3_REGION

    def __post_init__(self) -> None:
        """Reject command syntax and non-normalized object namespaces."""
        validate_identifier(self.remote_name, "resource_id")
        validate_identifier(self.bucket_name, "resource_id")
        validate_identifier(self.region, "resource_id")
        validate_canadian_b2_endpoint(self.endpoint, self.region)
        if (
            not self.base_prefix
            or self.base_prefix.startswith("/")
            or not self.base_prefix.endswith("/")
            or ".." in self.base_prefix.split("/")
        ):
            raise ValueError("base_prefix must be one normalized namespace")


@dataclass(frozen=True, slots=True)
class RcloneObjectEvidence:
    """Exact metadata recovered after an immutable B2 operation."""

    size_bytes: int
    sha256: str
    version_id: str


class RcloneB2Client:
    """Minimal immutable B2 operations through its S3-compatible API."""

    def __init__(
        self,
        executor: CommandExecutor,
        credentials: RcloneCredentials,
        location: RcloneLocation,
        *,
        executable: str = RCLONE_EXECUTABLE,
        transfer_timeout_seconds: float = TRANSFER_TIMEOUT_SECONDS,
        transfer_streams: int = TRANSFER_STREAMS,
    ) -> None:
        """Initialize from explicit command, secret, and scope dependencies."""
        if not executable.startswith("/"):
            raise ValueError("rclone executable must be absolute")
        if transfer_timeout_seconds <= 0:
            raise ValueError("transfer timeout must be positive")
        if transfer_streams < 1:
            raise ValueError("transfer streams must be positive")
        self._executor = executor
        self._credentials = credentials
        self._location = location
        self._executable = executable
        self._transfer_timeout_seconds = transfer_timeout_seconds
        self._transfer_streams = transfer_streams

    def _environment(self) -> dict[str, str]:
        remote = self._normalized_remote_name().upper()
        return {
            f"RCLONE_CONFIG_{remote}_TYPE": "s3",
            f"RCLONE_CONFIG_{remote}_PROVIDER": "Other",
            f"RCLONE_CONFIG_{remote}_ACCESS_KEY_ID": self._credentials.key_id,
            f"RCLONE_CONFIG_{remote}_SECRET_ACCESS_KEY": (
                self._credentials.application_key
            ),
            f"RCLONE_CONFIG_{remote}_ENDPOINT": self._location.endpoint,
            f"RCLONE_CONFIG_{remote}_REGION": self._location.region,
            f"RCLONE_CONFIG_{remote}_NO_CHECK_BUCKET": "true",
        }

    def _normalized_remote_name(self) -> str:
        """Return the environment-compatible rclone configuration section."""
        return self._location.remote_name.replace("-", "_")

    def _target(self, key: str | None = None) -> str:
        suffix = self._location.base_prefix
        if key is not None:
            logical_key = str(ObjectKey(key))
            suffix = (
                logical_key
                if logical_key.startswith(self._location.base_prefix)
                else suffix + logical_key
            )
        return f"{self._normalized_remote_name()}:{self._location.bucket_name}/{suffix}"

    def _arguments(self, *arguments: str) -> tuple[str, ...]:
        """Add the fixed empty config path so ambient files are never read."""
        return (self._executable, *arguments, "--config", "/dev/null")

    @staticmethod
    def _retry_arguments() -> tuple[str, ...]:
        """Return the retry bounds every multi-gigabyte transfer shares."""
        return (
            "--retries",
            "10",
            "--low-level-retries",
            "20",
            "--retries-sleep",
            "5s",
        )

    @staticmethod
    def _metadata_mapper_command(sha256: str) -> str:
        """Return the isolated mapper command in rclone's CSV-like syntax."""
        executable = '"' + sys.executable.replace('"', '""') + '"'
        return f"{executable} -m tkr_cloud_video.adapters.metadata_mapper {sha256}"

    async def probe(self) -> bool:
        """Prove only list access at the configured namespace."""
        await self._executor.run(
            self._arguments("lsjson", self._target(), "--max-depth", "1"),
            self._environment(),
            timeout_seconds=30,
        )
        return True

    async def head(self, key: str) -> RcloneObjectEvidence | None:
        """Return immutable SHA-256 metadata for one exact object."""
        try:
            result = await self._executor.run(
                self._arguments(
                    "lsjson",
                    self._target(key),
                    "--stat",
                    "--metadata",
                ),
                self._environment(),
                timeout_seconds=30,
            )
        except AppError as error:
            if error.code == "adapter_command_failed" and _not_found(error.__cause__):
                return None
            raise
        # B2 has no materialized directories, so rclone answers a stat for an
        # absent object with a success status rather than a not-found exit
        # code. It signals the absence in one of three ways: no output at all,
        # a null document, or a directory stat for the virtual parent. Callers
        # only pass validated object keys here, so each one means absent.
        payload = _optional_json_object(result.stdout)
        if payload is None or payload.get("IsDir") is True:
            return None
        metadata = payload.get("Metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        digest_value = metadata.get("sha256") or metadata.get("Sha256")
        if not isinstance(digest_value, str):
            raise AppError(
                "remote_digest_missing",
                "Remote object lacks required SHA-256 metadata.",
                context={"operation": "head_object"},
            )
        digest = str(Sha256Digest(digest_value))
        size = payload.get("Size")
        if not isinstance(size, int) or size < 0:
            raise AppError(
                "remote_metadata_invalid",
                "Remote object metadata is invalid.",
                context={"operation": "head_object"},
            )
        version = payload.get("ID") or payload.get("Version") or f"sha256-{digest}"
        if not isinstance(version, str):
            version = f"sha256-{digest}"
        return RcloneObjectEvidence(size, digest, version)

    async def put(self, key: str, content: bytes, sha256: str) -> RcloneObjectEvidence:
        """Create one immutable object and verify its remote metadata."""
        digest = str(Sha256Digest(sha256))
        if await self.head(key) is not None:
            raise AppError(
                "immutable_object_exists",
                "Immutable object already exists.",
                context={"operation": "put_object"},
            )
        await self._executor.run(
            self._arguments(
                "rcat",
                self._target(key),
                "--immutable",
                "--metadata",
                "--metadata-set",
                f"sha256={digest}",
            ),
            self._environment(),
            stdin=content,
        )
        evidence = await self.head(key)
        if evidence is None:
            raise AppError(
                "remote_verification_failed",
                "Created object was not visible for verification.",
                retryable=True,
                context={"operation": "put_object"},
            )
        return evidence

    async def put_url(
        self, key: str, url: str, sha256: str, size_bytes: int
    ) -> RcloneObjectEvidence:
        """Stream a seekable pinned HTTPS source into one immutable B2 object."""
        digest = str(Sha256Digest(sha256))
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "huggingface.co"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("artifact URL must use the approved Hugging Face host")
        source_path = parsed.path.lstrip("/")
        if not source_path or source_path.endswith("/"):
            raise ValueError("artifact URL must identify one source object")
        if size_bytes < 1:
            raise ValueError("artifact size must be positive")
        if await self.head(key) is not None:
            raise AppError(
                "immutable_object_exists",
                "Immutable object already exists.",
                context={"operation": "put_object"},
            )
        source_remote = f"{self._normalized_remote_name()}_source"
        source_environment_prefix = source_remote.upper()
        environment = {
            **self._environment(),
            f"RCLONE_CONFIG_{source_environment_prefix}_TYPE": "http",
            f"RCLONE_CONFIG_{source_environment_prefix}_URL": (
                "https://huggingface.co"
            ),
        }
        await self._executor.run(
            self._arguments(
                "copyto",
                f"{source_remote}:{source_path}",
                self._target(key),
                "--immutable",
                "--metadata",
                "--metadata-mapper",
                self._metadata_mapper_command(digest),
                *self._retry_arguments(),
            ),
            environment,
            timeout_seconds=self._transfer_timeout_seconds,
        )
        evidence = await self.head(key)
        if (
            evidence is None
            or evidence.sha256 != digest
            or evidence.size_bytes != size_bytes
        ):
            raise AppError(
                "remote_verification_failed",
                "Streamed object failed remote size or digest verification.",
                context={"operation": "put_object"},
            )
        return evidence

    async def get(self, key: str) -> bytes | None:
        """Read one exact private object, returning None only when absent."""
        try:
            result = await self._executor.run(
                self._arguments("cat", self._target(key)),
                self._environment(),
            )
        except AppError as error:
            if error.code == "adapter_command_failed" and _not_found(error.__cause__):
                return None
            raise
        return result.stdout

    def _prefix_target(self, prefix: str) -> str:
        """Return the remote target for a namespace rather than one key.

        ``ObjectKey`` rejects a trailing slash, so a prefix cannot be validated
        as a key. The namespace rules are the same ones ``CredentialScope``
        applies: normalized, relative, and unambiguous.
        """
        if (
            not prefix
            or prefix.startswith("/")
            or not prefix.endswith("/")
            or "//" in prefix
            or "\\" in prefix
            or any(
                part in {"", ".", ".."} for part in prefix.removesuffix("/").split("/")
            )
            or any(ord(character) < 32 for character in prefix)
        ):
            raise AppError(
                "invalid_boundary_value",
                "Listing prefix is not one normalized object namespace.",
                context={"field": "prefix"},
            )
        base = self._location.base_prefix
        suffix = prefix if prefix.startswith(base) else base + prefix
        remote = self._normalized_remote_name()
        return f"{remote}:{self._location.bucket_name}/{suffix}"

    async def list_objects(self, prefix: str) -> tuple[tuple[str, datetime], ...]:
        """List every object beneath a namespace with its modification time.

        Args:
            prefix: Normalized namespace ending in a slash.

        Returns:
            Key and modification time pairs. Directories are excluded: B2 has
            no materialized directories, and rclone's synthetic ones are not
            objects that can expire.

        """
        result = await self._executor.run(
            self._arguments(
                "lsjson",
                self._prefix_target(prefix),
                "--recursive",
                "--files-only",
            ),
            self._environment(),
            timeout_seconds=self._transfer_timeout_seconds,
        )
        listed: list[tuple[str, datetime]] = []
        for entry in _json_array(result.stdout):
            path = entry.get("Path")
            modified = entry.get("ModTime")
            if not isinstance(path, str) or not isinstance(modified, str):
                raise AppError(
                    "provider_response_invalid",
                    "Provider listing lacks a path or modification time.",
                    context={"operation": "list_objects"},
                )
            listed.append((f"{prefix}{path}", _provider_timestamp(modified)))
        return tuple(listed)

    async def delete(self, key: str) -> None:
        """Delete one exact object, treating an absent one as deleted."""
        try:
            await self._executor.run(
                self._arguments("deletefile", self._target(key)),
                self._environment(),
                timeout_seconds=30,
            )
        except AppError as error:
            if error.code == "adapter_command_failed" and _not_found(error.__cause__):
                return
            raise

    async def download(self, key: str, destination: Path) -> None:
        """Stream an exact object into a caller-owned partial path."""
        if destination.exists():
            raise ValueError("download destination must not exist")
        await self._executor.run(
            self._arguments(
                "copyto",
                self._target(key),
                str(destination),
                "--immutable",
                "--multi-thread-streams",
                str(self._transfer_streams),
                *self._retry_arguments(),
            ),
            self._environment(),
            timeout_seconds=self._transfer_timeout_seconds,
        )


class ArtifactRcloneStore:
    """ArtifactStore-compatible view over one model-reader client."""

    def __init__(self, client: RcloneB2Client) -> None:
        """Initialize a model artifact view over one scoped client."""
        self._client = client

    async def head(self, key: str) -> ObjectMetadata | None:
        """Return provider-neutral artifact metadata when present."""
        evidence = await self._client.head(key)
        if evidence is None:
            return None
        return ObjectMetadata(evidence.size_bytes, evidence.sha256, evidence.version_id)

    async def put(self, key: str, content: bytes, sha256: str) -> ObjectMetadata:
        """Create and verify one immutable artifact object."""
        evidence = await self._client.put(key, content, sha256)
        return ObjectMetadata(evidence.size_bytes, evidence.sha256, evidence.version_id)

    async def put_url(
        self, key: str, url: str, sha256: str, size_bytes: int
    ) -> ObjectMetadata:
        """Stream and verify one approved HTTPS artifact source."""
        evidence = await self._client.put_url(key, url, sha256, size_bytes)
        return ObjectMetadata(evidence.size_bytes, evidence.sha256, evidence.version_id)

    async def get(self, key: str) -> bytes:
        """Read exact artifact bytes or fail closed when absent."""
        content = await self._client.get(key)
        if content is None:
            raise AppError("object_not_found", "Private artifact does not exist.")
        return content


class ResultRcloneStore:
    """ResultStore-compatible view over one output-writer client."""

    def __init__(self, client: RcloneB2Client) -> None:
        """Initialize an output result view over one scoped client."""
        self._client = client

    async def head(self, key: str) -> RemoteMetadata | None:
        """Return provider-neutral result metadata when present."""
        evidence = await self._client.head(key)
        if evidence is None:
            return None
        return RemoteMetadata(evidence.size_bytes, evidence.sha256, evidence.version_id)

    async def put(self, key: str, content: bytes, sha256: str) -> RemoteMetadata:
        """Create and verify one immutable result object."""
        evidence = await self._client.put(key, content, sha256)
        return RemoteMetadata(evidence.size_bytes, evidence.sha256, evidence.version_id)

    async def get(self, key: str) -> bytes | None:
        """Read exact result bytes when present."""
        return await self._client.get(key)


class RcloneBlobDownloader:
    """BlobDownloader-compatible read-only streaming adapter."""

    def __init__(self, client: RcloneB2Client) -> None:
        """Initialize a streaming read view over one scoped client."""
        self._client = client

    async def download(self, object_key: str, destination: Path) -> None:
        """Stream one exact object into a caller-owned partial path."""
        await self._client.download(object_key, destination)


class RcloneInputSource:
    """Principal-contained InputSource view over one input-reader client."""

    def __init__(self, client: RcloneB2Client) -> None:
        """Initialize a private input view over one scoped client."""
        self._client = client

    async def authorized(self, principal_id: str, key: ObjectKey) -> bool:
        """Require the object to remain in the caller's owned namespace."""
        validate_identifier(principal_id, "resource_id")
        return str(key).startswith(f"principals/{principal_id}/")

    async def download(self, key: ObjectKey, destination: Path, max_bytes: int) -> int:
        """Download one authorized input and enforce its local byte bound."""
        await self._client.download(str(key), destination)
        size = destination.stat().st_size
        if size > max_bytes:
            destination.unlink(missing_ok=True)
            raise AppError(
                "input_size_invalid",
                "Private input exceeds policy.",
            )
        return size


def _optional_json_object(content: bytes) -> dict[str, Any] | None:
    """Parse a provider document, reporting an empty or null one as absent.

    Args:
        content: Raw provider output.

    Returns:
        The parsed object, or None when the provider described nothing.

    Raises:
        AppError: The output is neither empty, null, nor a JSON object.

    """
    if not content.strip():
        return None
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AppError(
            "provider_response_invalid",
            "Provider returned invalid metadata.",
            context={"operation": "parse_provider_response"},
            cause=error,
        ) from error
    if value is None:
        return None
    if not isinstance(value, dict):
        raise AppError(
            "provider_response_invalid",
            "Provider returned invalid metadata.",
            context={"operation": "parse_provider_response"},
        )
    return value


def _json_array(content: bytes) -> list[dict[str, Any]]:
    """Parse a provider listing, reporting empty output as no entries.

    Args:
        content: Raw provider output.

    Returns:
        The listed entries, empty when the namespace holds nothing.

    Raises:
        AppError: The output is neither empty nor a JSON array of objects.

    """
    if not content.strip():
        return []
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AppError(
            "provider_response_invalid",
            "Provider returned an invalid listing.",
            context={"operation": "parse_provider_response"},
            cause=error,
        ) from error
    if value is None:
        return []
    if not isinstance(value, list) or any(
        not isinstance(entry, dict) for entry in value
    ):
        raise AppError(
            "provider_response_invalid",
            "Provider returned an invalid listing.",
            context={"operation": "parse_provider_response"},
        )
    return value


def _provider_timestamp(value: str) -> datetime:
    """Parse a provider timestamp into an aware UTC instant.

    rclone reports nanosecond precision, which :func:`datetime.fromisoformat`
    does not accept, so the fraction is truncated to microseconds. Retention
    decisions are made in days; nanoseconds were never load-bearing.

    Args:
        value: Provider-reported ISO 8601 timestamp.

    Returns:
        The parsed instant, always timezone-aware.

    Raises:
        AppError: The timestamp cannot be parsed or carries no offset.

    """
    normalized = value.strip().replace("Z", "+00:00")
    if "." in normalized:
        head, _, tail = normalized.partition(".")
        # Only the digits leading the fraction belong to it. Collecting every
        # digit in the tail would swallow the offset's own digits and leave the
        # timestamp with no timezone at all.
        length = 0
        while length < len(tail) and tail[length].isdigit():
            length += 1
        normalized = f"{head}.{tail[:length][:6]}{tail[length:]}"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise AppError(
            "provider_response_invalid",
            "Provider returned an unparseable timestamp.",
            context={"operation": "parse_provider_response"},
            cause=error,
        ) from error
    if parsed.tzinfo is None:
        raise AppError(
            "provider_response_invalid",
            "Provider timestamp carries no timezone.",
            context={"operation": "parse_provider_response"},
        )
    return parsed.astimezone(UTC)


class RetentionRcloneStore:
    """RetentionStore view over one delete-capable control-plane client.

    The only store in the project that deletes. It is bound to the reaper
    credential and is never constructed inside a worker process.
    """

    def __init__(self, client: RcloneB2Client) -> None:
        """Initialize a retention view over one scoped client."""
        self._client = client

    async def list_objects(self, prefix: str) -> tuple[StoredObject, ...]:
        """Return every object under a prefix with its upload time."""
        return tuple(
            StoredObject(key, uploaded)
            for key, uploaded in await self._client.list_objects(prefix)
        )

    async def head_exists(self, key: str) -> bool:
        """Return whether one exact object exists."""
        return await self._client.head(key) is not None

    async def delete(self, key: str) -> None:
        """Delete one exact object."""
        await self._client.delete(key)


def _not_found(cause: BaseException | None) -> bool:
    if cause is None:
        return False
    message = str(cause).lower()
    return "not found" in message or "object_not_found" in message
