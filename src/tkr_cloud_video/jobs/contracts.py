"""Versioned discriminated MiniMax H3 generation request contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Annotated, Any, Final, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from tkr_cloud_video.core.context import validate_identifier
from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.security.validation import ModelSetId, ObjectKey, Sha256Digest

MAX_PROMPT_CHARS: Final[int] = 4000

# Verified against Comfy-Org/ComfyUI @ dec5d9450a5290bcf63430409ea41018e67f41c3,
# comfy_extras/nodes_minimax_h3.py — the revision this worker image pins. The node
# declares width=1344, height=768, length=124 as its own defaults, and its length
# tooltip states: "Frame count at 24 fps, snapped up to the model's 17k+5 grid
# (124 = ~5s; trained range is ~124-362, longer is untested)".
CANVAS_MULTIPLE: Final[int] = 32
TRAINED_SHORT_EDGE: Final[int] = 768
TRAINED_LONG_EDGE: Final[int] = 1344
TRAINED_MAX_PIXELS: Final[int] = TRAINED_SHORT_EDGE * TRAINED_LONG_EDGE
TRAINED_MIN_FRAMES: Final[int] = 124
TRAINED_MAX_FRAMES: Final[int] = 362
FRAME_GRID_STRIDE: Final[int] = 17
FRAME_GRID_OFFSET: Final[int] = 5


@dataclass(frozen=True, slots=True)
class TrainedEnvelope:
    """Where a request sits relative to the range the model was trained on.

    The floor is reported and the ceiling is enforced, deliberately. Below the
    trained range a request is cheaper and is a legitimate smoke test, so it stays
    legal and merely stops being silent. Above it a request is more expensive and
    the node's own tooltip calls it untested, so there is no cheap use to protect
    and :data:`TRAINED_MAX_FRAMES` is a hard field bound instead.

    Args:
        short_edge_below_trained: The short edge is under the 768 the model
            was trained at, so the canvas is smaller than any trained sample.
        frames_below_trained: Fewer frames than the trained floor.

    """

    short_edge_below_trained: bool
    frames_below_trained: bool

    @property
    def inside(self) -> bool:
        """Report whether the request sits wholly inside the trained range."""
        return not (self.short_edge_below_trained or self.frames_below_trained)

    def as_metadata(self) -> dict[str, bool]:
        """Return the envelope position for reproducibility metadata and events."""
        return {
            "trained_envelope_inside": self.inside,
            "trained_envelope_short_edge_below": self.short_edge_below_trained,
            "trained_envelope_frames_below": self.frames_below_trained,
        }


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
    prompt: str | None = Field(default=None, max_length=MAX_PROMPT_CHARS)
    structured_prompt: dict[str, Any] | None = None
    seed: int = Field(ge=0, le=18_446_744_073_709_551_615)
    width: int = Field(
        default=TRAINED_LONG_EDGE,
        ge=256,
        le=TRAINED_LONG_EDGE,
        multiple_of=CANVAS_MULTIPLE,
    )
    height: int = Field(
        default=TRAINED_SHORT_EDGE,
        ge=256,
        le=TRAINED_LONG_EDGE,
        multiple_of=CANVAS_MULTIPLE,
    )
    frames: int = Field(
        default=TRAINED_MIN_FRAMES, ge=FRAME_GRID_OFFSET, le=TRAINED_MAX_FRAMES
    )
    fps: Literal[24] = 24

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

    @field_validator("frames")
    @classmethod
    def validate_frame_grid(cls, value: int) -> int:
        """Require MiniMax H3's native 17k+5 temporal grid.

        The node snaps a non-conforming count up to the grid without saying so.
        Rejecting instead keeps the rendered frame count equal to the requested
        one, so a committed result's frame count is the number that was asked for.
        """
        if (value - FRAME_GRID_OFFSET) % FRAME_GRID_STRIDE != 0:
            raise ValueError("frames must satisfy MiniMax H3's 17k+5 grid")
        return value

    @model_validator(mode="after")
    def validate_exactly_one_prompt_form(self) -> RequestBase:
        """Require exactly one prompt form, with the freeform bound unchanged.

        The structured form is an addition, never a relaxation: a freeform prompt
        is still one to :data:`MAX_PROMPT_CHARS` characters, and supplying both
        forms is refused rather than resolved by precedence.
        """
        if (self.prompt is None) == (self.structured_prompt is None):
            raise ValueError("supply exactly one of prompt or structured_prompt")
        if self.prompt is not None and not self.prompt.strip():
            raise ValueError("prompt must not be blank")
        return self

    @model_validator(mode="after")
    def validate_native_canvas(self) -> RequestBase:
        """Keep requests within MiniMax H3's supported native canvas.

        A short edge of at most 768 and a long edge of at most 1344 together imply
        the node's 768*1344 pixel-area cap, so the area needs no separate check.
        """
        if (
            min(self.width, self.height) > TRAINED_SHORT_EDGE
            or max(self.width, self.height) > TRAINED_LONG_EDGE
        ):
            raise ValueError("resolution exceeds MiniMax H3's native canvas")
        return self

    def trained_envelope(self) -> TrainedEnvelope:
        """Report where this request sits relative to the trained range."""
        return TrainedEnvelope(
            short_edge_below_trained=min(self.width, self.height) < TRAINED_SHORT_EDGE,
            frames_below_trained=self.frames < TRAINED_MIN_FRAMES,
        )

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
