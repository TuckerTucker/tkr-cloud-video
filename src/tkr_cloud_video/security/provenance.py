"""Immutable release provenance manifest and drift verification."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tkr_cloud_video.core.context import validate_identifier
from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.security.validation import ModelSetId, Sha256Digest


class ReleaseAssuranceError(AppError):
    """Release provenance or approval evidence is missing, stale, or drifting."""


class ArtifactKind(StrEnum):
    """Executable and data inputs that make a worker release reproducible."""

    IMAGE = "image"
    PYTHON_LOCK = "python-lock"
    COMFYUI = "comfyui"
    CUSTOM_NODE = "custom-node"
    WORKFLOW = "workflow"
    MANIFEST = "manifest"
    MODEL = "model"


REQUIRED_ARTIFACT_KINDS = frozenset(ArtifactKind)


class ProvenanceArtifact(BaseModel):
    """Immutable identity and verification evidence for one release component."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    kind: ArtifactKind
    identity: str
    sha256: str
    verification_command: str

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        """Validate a safe component name."""
        return validate_identifier(value, "resource_id")

    @field_validator("identity", "verification_command")
    @classmethod
    def validate_evidence_text(cls, value: str) -> str:
        """Reject empty or control-bearing executable evidence."""
        if not value or any(ord(character) < 32 for character in value):
            raise ValueError("provenance evidence contains unsupported characters")
        forbidden = ("password=", "secret=", "token=", "authorization:")
        if any(marker in value.lower() for marker in forbidden):
            raise ValueError("provenance evidence contains secret-bearing syntax")
        return value

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        """Apply the shared canonical SHA-256 contract."""
        return str(Sha256Digest(value))


class ReleaseManifest(BaseModel):
    """Versioned, deterministic identity of every approved release artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1"
    release_id: str
    model_set_id: str
    created_at: datetime
    artifacts: tuple[ProvenanceArtifact, ...] = Field(min_length=7)

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: str) -> str:
        """Accept the current manifest schema only."""
        if value != "1":
            raise ValueError("unsupported release manifest schema")
        return value

    @field_validator("release_id")
    @classmethod
    def validate_release_id(cls, value: str) -> str:
        """Validate the public release identity."""
        return validate_identifier(value, "release_id")

    @field_validator("model_set_id")
    @classmethod
    def validate_model_set_id(cls, value: str) -> str:
        """Validate the pinned model-set identity."""
        return str(ModelSetId(value))

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        """Require an unambiguous timezone-aware creation time."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_artifact_completeness(self) -> Self:
        """Require every provenance class and unique component names."""
        names = [artifact.name for artifact in self.artifacts]
        if len(names) != len(set(names)):
            raise ValueError("artifact names must be unique")
        kinds = {artifact.kind for artifact in self.artifacts}
        missing = REQUIRED_ARTIFACT_KINDS.difference(kinds)
        if missing:
            raise ValueError("release manifest is missing required artifact kinds")
        return self

    def canonical_bytes(self) -> bytes:
        """Serialize deterministically for digesting and immutable storage."""
        payload = self.model_dump(mode="json")
        return json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")

    def digest(self) -> Sha256Digest:
        """Return the manifest's canonical SHA-256 identity."""
        return Sha256Digest(hashlib.sha256(self.canonical_bytes()).hexdigest())

    def version_identity(self) -> dict[str, str]:
        """Return the sanitized worker version-endpoint contract."""
        return {
            "release_id": self.release_id,
            "model_set_id": self.model_set_id,
            "manifest_digest": str(self.digest()),
        }

    def verify(self, observed_digests: Mapping[str, Sha256Digest]) -> None:
        """Fail promotion if any observed component is absent or drifting."""
        expected = {artifact.name: artifact.sha256 for artifact in self.artifacts}
        for name, digest in expected.items():
            observed = observed_digests.get(name)
            if observed is None or str(observed) != digest:
                raise ReleaseAssuranceError(
                    "provenance_mismatch",
                    "Release artifact does not match approved provenance.",
                    context={"resource_id": name},
                )
