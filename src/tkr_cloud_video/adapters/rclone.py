"""Backblaze B2 object storage through an isolated rclone process."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from tkr_cloud_video.adapters.process import CommandExecutor
from tkr_cloud_video.artifacts.publisher import ObjectMetadata
from tkr_cloud_video.core.context import validate_identifier
from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.delivery.uploader import RemoteMetadata
from tkr_cloud_video.security.validation import ObjectKey, Sha256Digest


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

    def __post_init__(self) -> None:
        """Reject command syntax and non-normalized object namespaces."""
        validate_identifier(self.remote_name, "resource_id")
        validate_identifier(self.bucket_name, "resource_id")
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
    """Minimal immutable object operations with no configuration file."""

    def __init__(
        self,
        executor: CommandExecutor,
        credentials: RcloneCredentials,
        location: RcloneLocation,
        *,
        executable: str = "/usr/bin/rclone",
    ) -> None:
        """Initialize from explicit command, secret, and scope dependencies."""
        if not executable.startswith("/"):
            raise ValueError("rclone executable must be absolute")
        self._executor = executor
        self._credentials = credentials
        self._location = location
        self._executable = executable

    def _environment(self) -> dict[str, str]:
        remote = self._location.remote_name.upper().replace("-", "_")
        return {
            f"RCLONE_CONFIG_{remote}_TYPE": "b2",
            f"RCLONE_CONFIG_{remote}_ACCOUNT": self._credentials.key_id,
            f"RCLONE_CONFIG_{remote}_KEY": self._credentials.application_key,
        }

    def _target(self, key: str | None = None) -> str:
        suffix = self._location.base_prefix
        if key is not None:
            logical_key = str(ObjectKey(key))
            suffix = (
                logical_key
                if logical_key.startswith(self._location.base_prefix)
                else suffix + logical_key
            )
        return f"{self._location.remote_name}:{self._location.bucket_name}/{suffix}"

    def _arguments(self, *arguments: str) -> tuple[str, ...]:
        """Add the fixed empty config path so ambient files are never read."""
        return (self._executable, *arguments, "--config", "/dev/null")

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
        payload = _json_object(result.stdout)
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
        """Stream a pinned HTTPS source into one immutable B2 object."""
        digest = str(Sha256Digest(sha256))
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "huggingface.co":
            raise ValueError("artifact URL must use the approved Hugging Face host")
        if size_bytes < 1:
            raise ValueError("artifact size must be positive")
        if await self.head(key) is not None:
            raise AppError(
                "immutable_object_exists",
                "Immutable object already exists.",
                context={"operation": "put_object"},
            )
        await self._executor.run(
            self._arguments(
                "copyurl",
                url,
                self._target(key),
                "--immutable",
                "--no-clobber",
                "--metadata-set",
                f"sha256={digest}",
            ),
            self._environment(),
            timeout_seconds=14_400,
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
            ),
            self._environment(),
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


def _json_object(content: bytes) -> dict[str, Any]:
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AppError(
            "provider_response_invalid",
            "Provider returned invalid metadata.",
            context={"operation": "parse_provider_response"},
            cause=error,
        ) from error
    if not isinstance(value, dict):
        raise AppError(
            "provider_response_invalid",
            "Provider returned invalid metadata.",
            context={"operation": "parse_provider_response"},
        )
    return value


def _not_found(cause: BaseException | None) -> bool:
    if cause is None:
        return False
    message = str(cause).lower()
    return "not found" in message or "object_not_found" in message
