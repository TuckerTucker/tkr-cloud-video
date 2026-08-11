"""Strict worker environment configuration."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tkr_cloud_video.core.context import validate_identifier
from tkr_cloud_video.core.storage import B2_S3_ENDPOINT, B2_S3_REGION
from tkr_cloud_video.security.validation import ModelSetId, Sha256Digest


class WorkerSettings(BaseModel):
    """Validated non-secret settings and secret references."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    release_id: str
    worker_id: str
    model_set_id: str
    manifest_digest: str
    bucket_name: str
    b2_s3_endpoint: str = B2_S3_ENDPOINT
    b2_s3_region: str = B2_S3_REGION
    rclone_remote_name: str = "tkr"
    model_prefix: str = "models/"
    model_key_id_secret_reference: str = "B2_MODEL_KEY_ID"  # noqa: S105
    model_application_key_secret_reference: str = "B2_MODEL_APPLICATION_KEY"  # noqa: S105
    input_prefix: str = "inputs/"
    input_key_id_secret_reference: str = "B2_INPUT_KEY_ID"  # noqa: S105
    input_application_key_secret_reference: str = "B2_INPUT_APPLICATION_KEY"  # noqa: S105
    output_prefix: str = "outputs/"
    output_key_id_secret_reference: str = "B2_OUTPUT_KEY_ID"  # noqa: S105
    output_application_key_secret_reference: str = "B2_OUTPUT_APPLICATION_KEY"  # noqa: S105
    principal_id: str = "runpod-endpoint"
    cache_root: Path
    model_root: Path
    workspace_root: Path
    output_root: Path
    comfyui_root: Path = Path("/opt/ComfyUI")
    python_executable: str = "/opt/venv/bin/python"
    comfyui_port: int = Field(default=8188, ge=1024, le=65535)
    maximum_input_bytes: int = Field(default=250_000_000, gt=0)
    generation_timeout_seconds: float = Field(default=1800, gt=0)
    disk_safety_bytes: int = Field(default=10_000_000_000, ge=0)
    startup_timeout_seconds: float = Field(default=300, gt=0)
    shutdown_timeout_seconds: float = Field(default=60, gt=0)

    @field_validator(
        "release_id",
        "worker_id",
        "bucket_name",
        "rclone_remote_name",
        "principal_id",
    )
    @classmethod
    def validate_identifiers(cls, value: str) -> str:
        """Validate non-secret runtime identities."""
        return validate_identifier(value, "resource_id")

    @field_validator("model_prefix", "input_prefix", "output_prefix")
    @classmethod
    def validate_prefix(cls, value: str) -> str:
        """Require one normalized credential-enforced namespace."""
        if (
            not value
            or value.startswith("/")
            or not value.endswith("/")
            or any(part in {"", ".", ".."} for part in value[:-1].split("/"))
        ):
            raise ValueError("model_prefix must be normalized")
        return value

    @field_validator("model_set_id")
    @classmethod
    def validate_model_set(cls, value: str) -> str:
        """Validate selected model-set identity."""
        return str(ModelSetId(value))

    @field_validator("manifest_digest")
    @classmethod
    def validate_manifest(cls, value: str) -> str:
        """Validate independently pinned manifest identity."""
        return str(Sha256Digest(value))

    def sanitized(self) -> dict[str, str | int | float]:
        """Return diagnostics excluding secret names and filesystem roots."""
        return {
            "model_set_id": self.model_set_id,
            "manifest_digest": self.manifest_digest,
            "release_id": self.release_id,
            "worker_id": self.worker_id,
            "disk_safety_bytes": self.disk_safety_bytes,
            "startup_timeout_seconds": self.startup_timeout_seconds,
            "shutdown_timeout_seconds": self.shutdown_timeout_seconds,
        }

    @property
    def manifest_key(self) -> str:
        """Return the exact digest-addressed key relative to the model prefix."""
        return f"manifests/sha256/{self.manifest_digest}.json"


WORKER_ENVIRONMENT_FIELDS = {
    "TKR_RELEASE_ID": "release_id",
    "TKR_WORKER_ID": "worker_id",
    "TKR_MODEL_SET_ID": "model_set_id",
    "TKR_MANIFEST_DIGEST": "manifest_digest",
    "TKR_B2_BUCKET_NAME": "bucket_name",
    "TKR_B2_S3_ENDPOINT": "b2_s3_endpoint",
    "TKR_B2_S3_REGION": "b2_s3_region",
    "TKR_RCLONE_REMOTE_NAME": "rclone_remote_name",
    "TKR_MODEL_PREFIX": "model_prefix",
    "TKR_MODEL_KEY_ID_SECRET_REFERENCE": "model_key_id_secret_reference",
    "TKR_MODEL_APPLICATION_KEY_SECRET_REFERENCE": (
        "model_application_key_secret_reference"
    ),
    "TKR_INPUT_PREFIX": "input_prefix",
    "TKR_INPUT_KEY_ID_SECRET_REFERENCE": "input_key_id_secret_reference",
    "TKR_INPUT_APPLICATION_KEY_SECRET_REFERENCE": (
        "input_application_key_secret_reference"
    ),
    "TKR_OUTPUT_PREFIX": "output_prefix",
    "TKR_OUTPUT_KEY_ID_SECRET_REFERENCE": "output_key_id_secret_reference",
    "TKR_OUTPUT_APPLICATION_KEY_SECRET_REFERENCE": (
        "output_application_key_secret_reference"
    ),
    "TKR_PRINCIPAL_ID": "principal_id",
    "TKR_CACHE_ROOT": "cache_root",
    "TKR_MODEL_ROOT": "model_root",
    "TKR_WORKSPACE_ROOT": "workspace_root",
    "TKR_OUTPUT_ROOT": "output_root",
    "TKR_COMFYUI_ROOT": "comfyui_root",
    "TKR_PYTHON_EXECUTABLE": "python_executable",
    "TKR_COMFYUI_PORT": "comfyui_port",
    "TKR_MAXIMUM_INPUT_BYTES": "maximum_input_bytes",
    "TKR_GENERATION_TIMEOUT_SECONDS": "generation_timeout_seconds",
    "TKR_DISK_SAFETY_BYTES": "disk_safety_bytes",
    "TKR_STARTUP_TIMEOUT_SECONDS": "startup_timeout_seconds",
    "TKR_SHUTDOWN_TIMEOUT_SECONDS": "shutdown_timeout_seconds",
}


def load_worker_settings(environment: Mapping[str, str]) -> WorkerSettings:
    """Parse only allowlisted worker variables with contained image defaults."""
    values: dict[str, Any] = {
        "cache_root": "/cache",
        "model_root": "/opt/ComfyUI/models",
        "workspace_root": "/workspaces",
        "output_root": "/outputs",
    }
    for environment_name, field_name in WORKER_ENVIRONMENT_FIELDS.items():
        if environment_name in environment:
            values[field_name] = environment[environment_name]
    return WorkerSettings.model_validate(values)
