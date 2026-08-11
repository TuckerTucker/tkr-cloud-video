"""Validated release catalog preparation for MiniMax H3 artifacts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Self
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tkr_cloud_video.artifacts.models import (
    ArtifactEntry,
    ArtifactRole,
    ModelSetManifest,
    WorkflowBinding,
)
from tkr_cloud_video.artifacts.streaming_publisher import (
    ArtifactSource,
    UrlArtifactSource,
)
from tkr_cloud_video.core.context import validate_identifier
from tkr_cloud_video.security.validation import (
    ModelSetId,
    RelativeDestination,
    Sha256Digest,
)


class CatalogOrigin(BaseModel):
    """Immutable upstream repository and licence evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    repository: str
    revision: str
    license_url: str

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        """Accept the reviewed upstream repository only."""
        if value != "Comfy-Org/MiniMax-H3":
            raise ValueError("unsupported artifact repository")
        return value

    @field_validator("revision")
    @classmethod
    def validate_revision(cls, value: str) -> str:
        """Require a full immutable Git revision."""
        invalid_character = any(
            character not in "0123456789abcdef" for character in value
        )
        if len(value) != 40 or invalid_character:
            raise ValueError("source revision must be a lowercase Git SHA-1")
        return value

    @field_validator("license_url")
    @classmethod
    def validate_license_url(cls, value: str) -> str:
        """Require the reviewed upstream MiniMax licence location."""
        parsed = urlparse(value)
        if parsed.scheme != "https" or parsed.hostname != "huggingface.co":
            raise ValueError("license URL must use the approved Hugging Face host")
        return value


class CatalogModel(BaseModel):
    """One digest-pinned model source and ComfyUI destination."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    source_url: str
    sha256: str
    size_bytes: int = Field(gt=0)
    destination: str

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        """Validate a stable catalog artifact identity."""
        return str(ModelSetId(value))

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: str) -> str:
        """Confine direct transfers to immutable Hugging Face URLs."""
        parsed = urlparse(value)
        if parsed.scheme != "https" or parsed.hostname != "huggingface.co":
            raise ValueError("model source must use the approved Hugging Face host")
        if "/resolve/" not in parsed.path:
            raise ValueError("model source must use a revision-pinned resolve URL")
        return value

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        """Validate the upstream Git LFS object identity."""
        return str(Sha256Digest(value))

    @field_validator("destination")
    @classmethod
    def validate_destination(cls, value: str) -> str:
        """Validate the model's contained ComfyUI-relative destination."""
        return str(RelativeDestination(value))


class CatalogWorkflow(BaseModel):
    """One repository-local API workflow and its approved bindings."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    path: str
    destination: str
    bindings: tuple[WorkflowBinding, ...] = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        """Validate the public workflow identity."""
        return validate_identifier(value, "resource_id")

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        """Require a single catalog-local file name."""
        path = PurePosixPath(value)
        if path.is_absolute() or len(path.parts) != 1 or path.suffix != ".json":
            raise ValueError("workflow path must be one catalog-local JSON file")
        return value

    @field_validator("destination")
    @classmethod
    def validate_destination(cls, value: str) -> str:
        """Validate the hydrated workflow destination."""
        return str(RelativeDestination(value))


class ReleaseCatalog(BaseModel):
    """Canonical operator-reviewed input for one model-set manifest."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: str = "1"
    model_set_id: str
    created_at: datetime
    source: CatalogOrigin
    models: tuple[CatalogModel, ...] = Field(min_length=1)
    workflow: CatalogWorkflow

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: str) -> str:
        """Accept only the current catalog schema."""
        if value != "1":
            raise ValueError("unsupported release catalog schema")
        return value

    @field_validator("model_set_id")
    @classmethod
    def validate_model_set_id(cls, value: str) -> str:
        """Validate the immutable model-set identity."""
        return str(ModelSetId(value))

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        """Require an unambiguous catalog timestamp."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("catalog timestamp must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_uniqueness_and_revision(self) -> Self:
        """Reject duplicate models and URLs not pinned to the catalog revision."""
        names = [model.name for model in self.models]
        destinations = [model.destination for model in self.models]
        if len(names) != len(set(names)) or len(destinations) != len(set(destinations)):
            raise ValueError("model names and destinations must be unique")
        revision_marker = f"/resolve/{self.source.revision}/"
        if any(revision_marker not in model.source_url for model in self.models):
            raise ValueError("model URL revision differs from catalog origin")
        return self

    def provenance_digest(self) -> str:
        """Return the canonical identity of the reviewed source catalog."""
        content = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
        return hashlib.sha256(content).hexdigest()


@dataclass(frozen=True, slots=True)
class PreparedModelSet:
    """Validated manifest plus publication sources and source catalogue."""

    catalog: ReleaseCatalog
    manifest: ModelSetManifest
    sources: dict[str, ArtifactSource]


def prepare_model_set(catalog_path: Path, license_approval_id: str) -> PreparedModelSet:
    """Build a deterministic model-set manifest from a reviewed local catalog."""
    approval_id = validate_identifier(license_approval_id, "resource_id")
    catalog = ReleaseCatalog.model_validate_json(catalog_path.read_bytes())
    workflow_path = catalog_path.resolve().parent / catalog.workflow.path
    workflow_content = workflow_path.read_bytes()
    _validate_api_workflow(workflow_content)
    workflow_digest = hashlib.sha256(workflow_content).hexdigest()
    model_entries = tuple(
        ArtifactEntry(
            name=model.name,
            role=ArtifactRole.MODEL,
            object_key=f"blobs/sha256/{model.sha256}",
            sha256=model.sha256,
            size_bytes=model.size_bytes,
            destination=model.destination,
        )
        for model in catalog.models
    )
    workflow_entry = ArtifactEntry(
        name=catalog.workflow.name,
        role=ArtifactRole.WORKFLOW,
        object_key=f"blobs/sha256/{workflow_digest}",
        sha256=workflow_digest,
        size_bytes=len(workflow_content),
        destination=catalog.workflow.destination,
        dependencies=tuple(entry.name for entry in model_entries),
        bindings=catalog.workflow.bindings,
    )
    manifest = ModelSetManifest(
        model_set_id=catalog.model_set_id,
        created_at=catalog.created_at,
        provenance_digest=catalog.provenance_digest(),
        license_approval_id=approval_id,
        artifacts=(*model_entries, workflow_entry),
    )
    sources: dict[str, ArtifactSource] = {
        model.name: UrlArtifactSource(model.source_url) for model in catalog.models
    }
    sources[catalog.workflow.name] = workflow_path
    return PreparedModelSet(catalog, manifest, sources)


def _validate_api_workflow(content: bytes) -> None:
    try:
        document = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("workflow must contain valid JSON") from error
    if not isinstance(document, dict) or not document:
        raise ValueError("workflow must be a non-empty API-format object")
    for node in document.values():
        if (
            not isinstance(node, dict)
            or not isinstance(node.get("class_type"), str)
            or not isinstance(node.get("inputs"), dict)
        ):
            raise ValueError("workflow must use ComfyUI API node objects")
