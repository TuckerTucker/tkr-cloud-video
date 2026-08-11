"""Versioned canonical durable result commit contracts."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tkr_cloud_video.core.context import validate_identifier
from tkr_cloud_video.security.validation import ObjectKey, Sha256Digest


class ResultRole(StrEnum):
    """Required committed result artifact roles."""

    VIDEO = "video"
    WORKFLOW = "workflow"
    GENERATION = "generation"


class RemoteArtifact(BaseModel):
    """Verified immutable remote identity for one exact local result artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    role: ResultRole
    remote_key: str
    size_bytes: int = Field(gt=0)
    sha256: str
    provider_version_id: str

    @field_validator("remote_key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        """Validate provider key separately from local paths."""
        return str(ObjectKey(value))

    @field_validator("sha256")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        """Validate canonical local/remote content identity."""
        return str(Sha256Digest(value))

    @field_validator("provider_version_id")
    @classmethod
    def validate_version(cls, value: str) -> str:
        """Require an immutable provider object version."""
        return validate_identifier(value, "resource_id")


class ResultManifest(BaseModel):
    """Final consumer-visible commit marker, published only after verification."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: str = "1"
    job_id: str
    attempt_id: str
    workflow_digest: str
    model_set_id: str
    request_hash: str
    committed_at: datetime
    artifacts: tuple[RemoteArtifact, ...] = Field(min_length=3)

    @field_validator("schema_version")
    @classmethod
    def validate_schema(cls, value: str) -> str:
        """Accept only the current result contract."""
        if value != "1":
            raise ValueError("unsupported result schema")
        return value

    @field_validator("job_id", "attempt_id", "model_set_id")
    @classmethod
    def validate_ids(cls, value: str) -> str:
        """Validate server-owned result identities."""
        return validate_identifier(value, "resource_id")

    @field_validator("workflow_digest", "request_hash")
    @classmethod
    def validate_digests(cls, value: str) -> str:
        """Validate reproducibility digests."""
        return str(Sha256Digest(value))

    @field_validator("committed_at")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        """Require an unambiguous commit time."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("committed_at must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_complete_roles(self) -> Self:
        """Require exactly one verified artifact for every required role."""
        roles = [artifact.role for artifact in self.artifacts]
        if set(roles) != set(ResultRole) or len(roles) != len(set(roles)):
            raise ValueError("result requires exactly one artifact per role")
        prefix = f"outputs/{self.job_id}/{self.attempt_id}/"
        if any(
            not artifact.remote_key.startswith(prefix) for artifact in self.artifacts
        ):
            raise ValueError("result artifacts must share the immutable attempt prefix")
        return self

    def canonical_bytes(self) -> bytes:
        """Serialize deterministic result.json bytes."""
        return json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()

    def digest(self) -> Sha256Digest:
        """Return the result marker content identity."""
        return Sha256Digest(hashlib.sha256(self.canonical_bytes()).hexdigest())

    @property
    def commit_key(self) -> str:
        """Return the final consumer-visible marker key."""
        return f"outputs/{self.job_id}/{self.attempt_id}/result.json"
