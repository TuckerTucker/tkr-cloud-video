"""Versioned discriminated MiniMax H3 generation request contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Annotated, Any, Final, Literal, Self, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationInfo,
    field_validator,
    model_validator,
)

from tkr_cloud_video.core.context import validate_identifier
from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.jobs.trained_envelope import (
    TRAINED_RANGES,
    EnvelopeDeparture,
    TrainedRange,
)
from tkr_cloud_video.security.validation import ModelSetId, ObjectKey, Sha256Digest

MAX_PROMPT_CHARS: Final[int] = 4000

# The floor below which no canvas is worth rendering for any model. Every other
# canvas and frame bound is a property of the model set the request names and is
# resolved from its trained envelope, never from a constant here — that is what
# keeps a second model set from silently inheriting MiniMax H3's numbers.
MIN_CANVAS_EDGE: Final[int] = 256

RANGES_CONTEXT_KEY: Final[str] = "trained_ranges"


def _ranges_from(context: object) -> Mapping[str, TrainedRange]:
    """Return the envelope population a validation run was handed.

    The population is injected through pydantic's validation context so parsing
    stays a pure function of what it is given, and a test can drive it with a
    population of one. Absent a context, the pinned registry is used.
    """
    if isinstance(context, Mapping):
        injected = context.get(RANGES_CONTEXT_KEY)
        if isinstance(injected, Mapping):
            return injected
    return TRAINED_RANGES


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
    width: int = Field(ge=MIN_CANVAS_EDGE)
    height: int = Field(ge=MIN_CANVAS_EDGE)
    frames: int = Field(ge=1)
    fps: Literal[24] = 24

    @model_validator(mode="before")
    @classmethod
    def apply_trained_defaults(cls, data: object, info: ValidationInfo) -> object:
        """Fill absent dimensions from the trained range of the named model set.

        A request that names no dimensions has to land somewhere, and the only
        defensible somewhere is where the model it names was trained. Filling
        here rather than through static field defaults is what lets a second
        model set land on its own numbers instead of MiniMax H3's.

        A payload whose model set is missing or malformed is returned untouched,
        so the field validator reports that defect rather than having it masked
        by a failure to fill. An unregistered model set is refused here rather
        than after field validation, so it reads as one named defect instead of
        three "field required" errors for the dimensions that could not be
        resolved.
        """
        if not isinstance(data, dict):
            return data
        model_set_id = data.get("model_set_id")
        if not isinstance(model_set_id, str):
            return data
        try:
            normalized = str(ModelSetId(model_set_id))
        except AppError:
            return data
        entry = _ranges_from(info.context).get(normalized)
        if entry is None:
            raise ValueError("no trained envelope is registered for this model set")
        filled = dict(data)
        for field, value in (
            ("width", entry.default_width),
            ("height", entry.default_height),
            ("frames", entry.default_frames),
        ):
            if filled.get(field) is None:
                filled[field] = value
        return filled

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
    def validate_against_trained_range(self, info: ValidationInfo) -> Self:
        """Bound the request by the envelope of the model set it names.

        The ceiling is enforced and the floor is only reported, deliberately.
        Below the trained range a request is cheaper and is a legitimate smoke
        test, so it stays legal and merely stops being silent. Above it the
        node's own tooltip calls the result untested, so there is no cheap use
        to protect.

        The lookup cannot miss: :meth:`apply_trained_defaults` runs first on
        every construction path and refuses an unregistered model set there, so
        by here the envelope is known to exist.
        """
        entry = _ranges_from(info.context)[self.model_set_id]
        for axis in (self.width, self.height):
            if axis % entry.canvas_multiple != 0:
                raise ValueError(
                    f"canvas edges must be a multiple of {entry.canvas_multiple}"
                )
        if not entry.within_canvas(self.width, self.height):
            raise ValueError("resolution exceeds the model set's native canvas")
        if not entry.on_frame_grid(self.frames):
            raise ValueError(
                f"frames must satisfy the model set's "
                f"{entry.grid_stride}k+{entry.grid_offset} grid"
            )
        if not entry.grid_offset <= self.frames <= entry.max_frames:
            raise ValueError("frames outside the model set's supported range")
        return self

    def trained_envelope(
        self, ranges: Mapping[str, TrainedRange] | None = None
    ) -> EnvelopeDeparture:
        """Report where this request sits relative to its model's trained range.

        Args:
            ranges: Envelope population to measure against. Defaults to the
                pinned registry; a caller that parsed with an injected
                population passes the same one here, so the report cannot be
                read against a different model's numbers than the request was
                validated by.

        Raises:
            KeyError: The model set has no envelope in ``ranges``. Validation
                already refused an unregistered model set, so this can only
                mean a caller passed a different population than it parsed with.

        """
        entry = (TRAINED_RANGES if ranges is None else ranges)[self.model_set_id]
        return entry.departure(self.width, self.height, self.frames)

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


def parse_generation_request(
    payload: object, ranges: Mapping[str, TrainedRange] | None = None
) -> GenerationRequest:
    """Parse an external payload through the discriminated strict schema.

    Args:
        payload: The untrusted request payload.
        ranges: Trained-envelope population to resolve defaults and bounds
            against. Defaults to the pinned registry; injected so a caller can
            parse against a population of one without substituting the module.

    Returns:
        The validated request, with any absent dimension filled from the
        trained envelope of the model set it names.

    """
    return REQUEST_ADAPTER.validate_python(
        payload, context={RANGES_CONTEXT_KEY: ranges or TRAINED_RANGES}
    )
