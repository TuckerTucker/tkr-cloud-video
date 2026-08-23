"""Local intake preflight and the form constraints a caller needs to satisfy it.

The console runs the worker's own validation before it submits anything. That is
the single largest thing a UI can do for this product: a request that names an
off-grid frame count or breaks a closed prompt vocabulary is refused here in
milliseconds, where the same request submitted blind costs a cold start first.
`docs/audits/2026-08-15-generation-latency.md` measures that overhead at about
seven minutes.

The rejection this produces is deliberately the same shape the handler's own
rejection is, so the page renders a local refusal and a remote one identically
and neither has a privileged presentation.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from typing import Any, Final

from pydantic import ValidationError

from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.jobs.contracts import (
    MAX_PROMPT_CHARS,
    GenerationRequest,
    parse_generation_request,
)
from tkr_cloud_video.jobs.trained_envelope import (
    ENVELOPE_REVISION,
    TRAINED_RANGES,
    TrainedRange,
)
from tkr_cloud_video.prompt_authoring.composition import (
    PromptServices,
    resolve_request_prompt,
)
from tkr_cloud_video.prompting.errors import PromptValidationError
from tkr_cloud_video.prompting.grammar import GRAMMAR_REVISION, VOCABULARIES


@dataclass(frozen=True, slots=True)
class Rejection:
    """A refused request, in the shape the worker refuses one.

    Args:
        error_code: The stable code, which is the prompt error's own code when
            a closed vocabulary or a structural rule was broken.
        message: The error's safe message.
        error_context: Allowlisted context naming the field or rule at fault.
        defects: Every field-scoped prompt defect, so a caller corrects them in
            one pass rather than once per round trip.

    """

    error_code: str
    message: str
    error_context: Mapping[str, str | int | float | bool | None] | None = None
    defects: tuple[Mapping[str, str], ...] = ()

    def as_payload(self) -> dict[str, object]:
        """Return the rejection as the page's error document."""
        return {
            "ok": False,
            "error_code": self.error_code,
            "message": self.message,
            "error_context": dict(self.error_context) if self.error_context else None,
            "defects": [dict(defect) for defect in self.defects],
        }


@dataclass(frozen=True, slots=True)
class Accepted:
    """A request that would be accepted, with what the caller should see first.

    Args:
        request: The validated request, with every absent dimension filled from
            the trained envelope of the model set it names.
        request_hash: The canonical hash the job identity is derived from.
        wire_text: The exact text that would reach the workflow. Shown back to
            the caller because a structured prompt is rendered, not supplied,
            and the caller has otherwise no way to see what it composed to.
        below_trained_envelope: Whether the request sits under the model's
            trained range. Permitted and cheap, so it is reported rather than
            refused.

    """

    request: GenerationRequest
    request_hash: str
    wire_text: str
    below_trained_envelope: bool

    def as_payload(self) -> dict[str, object]:
        """Return the acceptance as the page's preflight document.

        The wire text is returned to the caller that supplied the prompt it was
        rendered from, and to nobody else. It is not logged and does not enter
        an error context, because it is the prompt.
        """
        departure = self.request.trained_envelope()
        return {
            "ok": True,
            "request": self.request.model_dump(mode="json"),
            "request_hash": self.request_hash,
            "wire_text": self.wire_text,
            "wire_text_length": len(self.wire_text),
            "wire_text_limit": MAX_PROMPT_CHARS,
            "below_trained_envelope": self.below_trained_envelope,
            "trained_envelope": {
                "inside": departure.inside,
                "short_edge_below_trained": departure.short_edge_below_trained,
                "frames_below_trained": departure.frames_below_trained,
                "revision": ENVELOPE_REVISION,
            },
        }


def preflight(payload: object, prompts: PromptServices) -> Accepted | Rejection:
    """Validate one request exactly as the worker's handler validates it.

    The two steps and their order are the handler's: parse against the
    discriminated request contract, then compose and render the prompt. The
    order matters, because composing a prompt for an unparseable request would
    report the wrong defect first.

    Args:
        payload: The untrusted ``input`` document.
        prompts: The composed prompt boundary.

    Returns:
        The accepted request and what to show for it, or the rejection the
        worker would have returned.

    """
    try:
        request = parse_generation_request(payload)
    except ValidationError as error:
        return Rejection(
            error_code="invalid_request",
            message="The request does not satisfy the generation contract.",
            defects=tuple(_field_defects(error)),
        )
    try:
        resolved = resolve_request_prompt(request, prompts)
    except PromptValidationError as error:
        return Rejection(
            error_code=error.code,
            message=str(error),
            error_context=dict(error.context) or None,
            defects=tuple(
                {**defect.as_context(), "message": ""} for defect in error.defects
            ),
        )
    except AppError as error:
        return Rejection(
            error_code=error.code,
            message=str(error),
            error_context=dict(error.context) or None,
        )
    return Accepted(
        request=request,
        request_hash=request.request_hash(),
        wire_text=resolved.text,
        below_trained_envelope=not request.trained_envelope().inside,
    )


# The discriminated union tags every error with the mode it resolved to, so a
# request-level failure arrives located at "text-to-video" rather than at a
# field. That is the union's bookkeeping, not a place in the caller's document.
_DISCRIMINATOR_TAGS: Final[frozenset[str]] = frozenset(
    {"text-to-video", "image-to-video", "reference-to-video"}
)


def _field_defects(error: ValidationError) -> list[dict[str, str]]:
    """Render pydantic's errors as field-scoped defects.

    The location, the rule and the message cross; the offending value never
    does. Pydantic keeps the input under its own key rather than interpolating
    it into the message, and every message reachable here is either written by
    this project's own validators or drawn from pydantic's fixed vocabulary, so
    carrying it costs nothing and is the whole diagnostic: a request refused for
    an off-grid frame count is located at the union's discriminator, and only
    the message says which rule the number broke.
    """
    defects: list[dict[str, str]] = []
    for detail in error.errors():
        parts = [
            str(part) for part in detail["loc"] if str(part) not in _DISCRIMINATOR_TAGS
        ]
        defects.append(
            {
                "field": ".".join(parts) or "request",
                "rule": detail["type"],
                "message": _strip_prefix(detail["msg"]),
            }
        )
    return defects


def _strip_prefix(message: str) -> str:
    """Drop pydantic's own "Value error, " prefix from a validator message."""
    return message.removeprefix("Value error, ")


def _describe_range(entry: TrainedRange) -> dict[str, object]:
    """Describe one model set's envelope as the form's own constraints."""
    return {
        "model_set_id": entry.model_set_id,
        "default_width": entry.default_width,
        "default_height": entry.default_height,
        "default_frames": entry.default_frames,
        "max_long_edge": entry.long_edge,
        "max_short_edge": entry.short_edge,
        "canvas_multiple": entry.canvas_multiple,
        "grid_stride": entry.grid_stride,
        "grid_offset": entry.grid_offset,
        "min_trained_frames": entry.min_frames,
        "max_frames": entry.max_frames,
        "fps": 24,
        "source": entry.source,
    }


def structured_example() -> dict[str, Any]:
    """Return a valid structured prompt for the form to start from.

    A form seeded with the fill-in template in `docs/prompting/templates` would
    open on a document that cannot pass validation, so it is seeded with a
    prompt that does. The example is packaged rather than read from the
    checkout, and `tests/console` pins it against the request file it came from
    so the two cannot drift apart unnoticed.
    """
    payload = json.loads(
        resources.files("tkr_cloud_video.console")
        .joinpath("static", "structured-example.json")
        .read_text()
    )
    return dict(payload)


def form_constraints() -> dict[str, Any]:
    """Return everything the page needs to build a satisfiable request.

    Serving the envelope rather than restating it in the page is what keeps the
    form honest: a second model set, or a corrected bound, reaches the UI by
    being registered, not by someone remembering to edit a script.
    """
    return {
        "model_sets": [
            _describe_range(entry)
            for entry in sorted(
                TRAINED_RANGES.values(), key=lambda entry: entry.model_set_id
            )
        ],
        "envelope_revision": ENVELOPE_REVISION,
        "grammar_revision": GRAMMAR_REVISION,
        "max_prompt_chars": MAX_PROMPT_CHARS,
        "structured_example": structured_example(),
        "vocabularies": [
            {
                "name": vocabulary.name,
                "values": list(vocabulary.values),
                "omitted_default": vocabulary.omitted_default,
            }
            for vocabulary in VOCABULARIES
        ],
    }
