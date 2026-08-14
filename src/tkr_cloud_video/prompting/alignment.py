"""Keyframe alignment headers for the modes that anchor a reference picture.

The header is the first line of a rendered prompt and is followed by one blank
line before the core sections. T2VA has no header: it anchors nothing.
"""

from __future__ import annotations

from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.prompting.models import StructuredPrompt

_OPENING_MARK = 0.0


class PromptAlignmentError(AppError):
    """A keyframe alignment mark cannot be expressed for the requested clip."""


def format_mark(seconds: float) -> str:
    """Return a timing mark formatted to exactly two decimal places."""
    return f"{seconds:.2f}"


def alignment_header(prompt: StructuredPrompt) -> str:
    """Return the keyframe alignment header for a prompt, or an empty string.

    Args:
        prompt: The composed prompt whose mode selects the header form.

    Returns:
        The header line, or an empty string for modes that anchor no picture.

    Raises:
        PromptAlignmentError: The closing mark falls outside the requested clip
            duration, so the header would instruct an alignment the clip cannot
            contain.

    """
    mode = prompt.mode
    if mode in ("T2VA", "Ref2VA"):
        return ""

    closing = prompt.duration_seconds
    final_shot = prompt.shots[-1].number
    _require_expressible_mark(closing)
    _require_mark_after_final_cut(closing, prompt)

    if mode == "I2VA":
        return (
            "For the target video, at "
            f"{format_mark(_OPENING_MARK)} seconds into the target video, "
            "<Picture 1> (from [Shot 1]) is fully referenced."
        )
    if mode == "FL2VA":
        return (
            "How the reference pictures align with the target video — "
            f"Picture 1 (from Shot 1) aligns with the {format_mark(_OPENING_MARK)}"
            "-second mark of the target video; "
            f"Picture 2 (from Shot {final_shot}) aligns with the "
            f"{format_mark(closing)}-second mark of the target video."
        )
    return (
        "How the reference pictures align with the target video — "
        f"<Picture 1> (from [Shot {final_shot}]) aligns with the "
        f"{format_mark(closing)}-second mark of the target video."
    )


def _require_expressible_mark(mark: float) -> None:
    """Reject a mark that two-decimal notation cannot state without rounding.

    The header instructs an exact alignment. Emitting ``8.00`` for a clip of
    8.005 seconds would state an instruction the caller did not ask for, so the
    duration is rejected rather than quietly rounded.
    """
    if abs(round(mark, 2) - mark) > 1e-9:
        raise PromptAlignmentError(
            "alignment_time_not_expressible",
            "Clip duration cannot be stated exactly in two-decimal notation.",
            context={"field": "duration_seconds"},
        )


def _require_mark_after_final_cut(mark: float, prompt: StructuredPrompt) -> None:
    """Reject a closing keyframe that lands at or before its own shot's cut.

    The closing picture belongs to the final shot. A mark at or before that
    shot's cut time would anchor the picture outside the shot that contains it.
    """
    final_cut = prompt.shots[-1].cut_at_seconds
    if final_cut is not None and mark <= final_cut:
        raise PromptAlignmentError(
            "alignment_time_outside_duration",
            "The closing keyframe mark is not inside the final shot.",
            context={"field": "cut_at_seconds", "rule": format_mark(mark)},
        )
