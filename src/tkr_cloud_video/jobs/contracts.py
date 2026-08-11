"""Versioned discriminated MiniMax H3 generation request contracts."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from tkr_cloud_video.core.context import validate_identifier
from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.security.validation import ModelSetId, ObjectKey, Sha256Digest


class InputReference(BaseModel):
    """Authorized private input identity without a user-controlled local name."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    object_key: str
    sha256: str | None = None

    @field_validator("object_key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        """Validate an object-store key independently from filesystem paths."""
        try:
            return str(ObjectKey(value))
        except AppError as error:
            raise ValueError("invalid object key") from error

    @field_validator("sha256")
    @classmethod
    def validate_digest(cls, value: str | None) -> str | None:
        """Validate an optional expected input digest."""
        if value is None:
            return None
        try:
            return str(Sha256Digest(value))
        except AppError as error:
            raise ValueError("invalid SHA-256 digest") from error


class RequestBase(BaseModel):
    """Shared normalized generation fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1"] = "1"
    workflow_id: str
    model_set_id: str
    prompt: str = Field(min_length=1, max_length=4000)
    seed: int = Field(ge=0, le=18_446_744_073_709_551_615)
    width: int = Field(default=1280, ge=256, le=2048, multiple_of=16)
    height: int = Field(default=720, ge=256, le=2048, multiple_of=16)
    frames: int = Field(default=81, ge=1, le=721)
    fps: int = Field(default=24, ge=1, le=60)

    @field_validator("workflow_id")
    @classmethod
    def validate_workflow(cls, value: str) -> str:
        """Validate approved workflow identity syntax."""
        try:
            return validate_identifier(value, "resource_id")
        except AppError as error:
            raise ValueError("invalid workflow identifier") from error

    @field_validator("model_set_id")
    @classmethod
    def validate_model_set(cls, value: str) -> str:
        """Validate selected model-set identity syntax."""
        try:
            return str(ModelSetId(value))
        except AppError as error:
            raise ValueError("invalid model-set identifier") from error

    def canonical_bytes(self) -> bytes:
        """Serialize a deterministic normalized request."""
        return json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()

    def request_hash(self) -> str:
        """Return the principal-independent canonical request hash."""
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class TextToVideoRequest(RequestBase):
    """Text-only generation request."""

    mode: Literal["text-to-video"]


class ImageToVideoRequest(RequestBase):
    """Single-image-conditioned generation request."""

    mode: Literal["image-to-video"]
    image: InputReference


class ReferenceToVideoRequest(RequestBase):
    """One-to-four-reference conditioned generation request."""

    mode: Literal["reference-to-video"]
    references: tuple[InputReference, ...] = Field(min_length=1, max_length=4)


GenerationRequest: TypeAlias = Annotated[
    TextToVideoRequest | ImageToVideoRequest | ReferenceToVideoRequest,
    Field(discriminator="mode"),
]
REQUEST_ADAPTER: TypeAdapter[GenerationRequest] = TypeAdapter(GenerationRequest)


def parse_generation_request(payload: object) -> GenerationRequest:
    """Parse an external payload through the discriminated strict schema."""
    return REQUEST_ADAPTER.validate_python(payload)
