"""Versioned model-set manifest contracts."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tkr_cloud_video.security.validation import (
    ModelSetId,
    ObjectKey,
    RelativeDestination,
    Sha256Digest,
)


class ArtifactRole(StrEnum):
    """Artifact placement and runtime purpose."""

    MODEL = "model"
    WORKFLOW = "workflow"


class WorkflowBindingSource(StrEnum):
    """Allowlisted values an approved workflow may receive at execution."""

    PROMPT = "prompt"
    SEED = "seed"
    WIDTH = "width"
    HEIGHT = "height"
    FRAMES = "frames"
    FPS = "fps"
    INPUT_0 = "input_0"
    INPUT_1 = "input_1"
    INPUT_2 = "input_2"
    INPUT_3 = "input_3"
    OUTPUT_PREFIX = "output_prefix"


class WorkflowBinding(BaseModel):
    """One release-approved value-to-node-input mapping."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    source: WorkflowBindingSource
    node_id: str
    input_name: str

    @field_validator("node_id", "input_name")
    @classmethod
    def validate_binding_identifier(cls, value: str) -> str:
        """Reject path/control syntax in graph-local identifiers."""
        return str(ModelSetId(value))


class ArtifactEntry(BaseModel):
    """One immutable remote blob and its validated local destination."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    role: ArtifactRole
    object_key: str
    sha256: str
    size_bytes: int = Field(gt=0)
    destination: str
    dependencies: tuple[str, ...] = ()
    bindings: tuple[WorkflowBinding, ...] = ()

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        """Validate a stable artifact name."""
        return str(ModelSetId(value))

    @field_validator("object_key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        """Validate the immutable remote key and digest-addressed layout."""
        key = str(ObjectKey(value))
        return key

    @field_validator("sha256")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        """Validate the canonical content digest."""
        return str(Sha256Digest(value))

    @field_validator("destination")
    @classmethod
    def validate_destination(cls, value: str) -> str:
        """Validate the ComfyUI-relative materialization destination."""
        return str(RelativeDestination(value))

    @model_validator(mode="after")
    def validate_content_key(self) -> Self:
        """Require the object name itself to contain the content digest."""
        if self.sha256 not in self.object_key:
            raise ValueError("artifact object key must contain its SHA-256 digest")
        if self.role is ArtifactRole.MODEL and self.bindings:
            raise ValueError("model artifacts cannot declare workflow bindings")
        targets = [(binding.node_id, binding.input_name) for binding in self.bindings]
        if len(targets) != len(set(targets)):
            raise ValueError("workflow binding targets must be unique")
        return self


class ModelSetManifest(BaseModel):
    """Canonical immutable description of one reproducible model set."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1"
    model_set_id: str
    created_at: datetime
    provenance_digest: str
    license_approval_id: str
    artifacts: tuple[ArtifactEntry, ...] = Field(min_length=2)

    @field_validator("schema_version")
    @classmethod
    def validate_schema(cls, value: str) -> str:
        """Accept only the current schema."""
        if value != "1":
            raise ValueError("unsupported model-set manifest schema")
        return value

    @field_validator("model_set_id", "license_approval_id")
    @classmethod
    def validate_ids(cls, value: str) -> str:
        """Validate stable release catalog identities."""
        return str(ModelSetId(value))

    @field_validator("provenance_digest")
    @classmethod
    def validate_provenance_digest(cls, value: str) -> str:
        """Validate the independently approved release provenance identity."""
        return str(Sha256Digest(value))

    @field_validator("created_at")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        """Require an unambiguous manifest creation time."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        """Require unique rows and complete workflow dependencies."""
        names = [entry.name for entry in self.artifacts]
        destinations = [entry.destination for entry in self.artifacts]
        if len(names) != len(set(names)) or len(destinations) != len(set(destinations)):
            raise ValueError("artifact names and destinations must be unique")
        known = set(names)
        for entry in self.artifacts:
            if not set(entry.dependencies).issubset(known):
                raise ValueError("artifact dependency is missing")
        if not any(entry.role is ArtifactRole.WORKFLOW for entry in self.artifacts):
            raise ValueError("manifest requires a workflow artifact")
        return self

    def canonical_bytes(self) -> bytes:
        """Serialize canonical JSON used as the manifest identity."""
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()

    def digest(self) -> Sha256Digest:
        """Return the canonical manifest digest."""
        return Sha256Digest(hashlib.sha256(self.canonical_bytes()).hexdigest())
