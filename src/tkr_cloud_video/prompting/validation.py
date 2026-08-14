"""Structural validation of a composed prompt, before any GPU work begins.

Each check declares the modes it applies to, and a result reports both the checks
that ran and the checks that did not. A validator that cannot say what it skipped
cannot distinguish "found nothing" from "looked at nothing", so the skipped
inventory is part of the result rather than a debugging aid.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from tkr_cloud_video.prompting.errors import PromptDefect, PromptValidationError
from tkr_cloud_video.prompting.grammar import (
    AUDIO_RETENTION_MARKERS,
    CAMERA_AMPLITUDES,
    CAMERA_MOTION_TYPES,
    CAMERA_SPEEDS,
    CUT_PHRASES,
    DIALOGUE_CLOSE,
    DIALOGUE_OPEN,
    TASK_TYPES,
    VISUAL_RETENTION_MARKERS,
    VISUAL_STYLES,
)
from tkr_cloud_video.prompting.models import StructuredPrompt

ALL_MODES: Final[frozenset[str]] = frozenset(
    {"T2VA", "I2VA", "FL2VA", "L2VA", "Ref2VA"}
)
REFERENCE_MODE: Final[frozenset[str]] = frozenset({"Ref2VA"})
AUDIO_LABEL_PREFIX: Final[str] = "<Audio "


@dataclass(frozen=True, slots=True)
class SkippedCheck:
    """A check that did not run, and the reason it does not apply."""

    rule: str
    reason: str


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """The outcome of validating one prompt.

    Args:
        defects: Every field-scoped defect found, in check order.
        checks_run: Rules exercised for the submitted mode.
        checks_skipped: Rules not exercised, each with why it does not apply.

    """

    defects: tuple[PromptDefect, ...]
    checks_run: tuple[str, ...]
    checks_skipped: tuple[SkippedCheck, ...]

    @property
    def accepted(self) -> bool:
        """Report whether the prompt carries no structural defect."""
        return not self.defects


@dataclass(frozen=True, slots=True)
class Check:
    """One structural check bound to the modes it applies to."""

    rule: str
    modes: frozenset[str]
    run: Callable[[StructuredPrompt], tuple[PromptDefect, ...]]


def _opening_shot_declares_no_cut(
    prompt: StructuredPrompt,
) -> tuple[PromptDefect, ...]:
    """Require no timestamp on the opening shot and one on every later shot."""
    defects: list[PromptDefect] = []
    for index, shot in enumerate(prompt.shots):
        has_cut = shot.cut_at_seconds is not None
        if index == 0 and has_cut:
            defects.append(
                PromptDefect("opening_shot_cut_time", "shots.0.cut_at_seconds")
            )
        if index > 0 and not has_cut:
            defects.append(
                PromptDefect(
                    "later_shot_missing_cut_time", f"shots.{index}.cut_at_seconds"
                )
            )
    return tuple(defects)


def _shot_numbering_is_sequential(
    prompt: StructuredPrompt,
) -> tuple[PromptDefect, ...]:
    """Require shot numbers to start at one and increase by one."""
    return tuple(
        PromptDefect("shot_numbering_not_sequential", f"shots.{index}.number")
        for index, shot in enumerate(prompt.shots)
        if shot.number != index + 1
    )


def _cut_times_strictly_increase(
    prompt: StructuredPrompt,
) -> tuple[PromptDefect, ...]:
    """Require each cut to happen strictly after the one before it."""
    defects: list[PromptDefect] = []
    previous: float | None = None
    for index, shot in enumerate(prompt.shots):
        current = shot.cut_at_seconds
        if current is None:
            continue
        if previous is not None and current <= previous:
            defects.append(
                PromptDefect("cut_time_not_increasing", f"shots.{index}.cut_at_seconds")
            )
        previous = current
    return tuple(defects)


def _cut_times_are_inside_the_clip(
    prompt: StructuredPrompt,
) -> tuple[PromptDefect, ...]:
    """Reject a cut at or past the end of the clip, which can never be reached."""
    return tuple(
        PromptDefect("cut_time_outside_duration", f"shots.{index}.cut_at_seconds")
        for index, shot in enumerate(prompt.shots)
        if shot.cut_at_seconds is not None
        and shot.cut_at_seconds >= prompt.duration_seconds
    )


def _delimiters_are_balanced(prompt: StructuredPrompt) -> tuple[PromptDefect, ...]:
    """Reject a dialogue delimiter inside any service-authored span."""
    defects: list[PromptDefect] = []
    for index, shot in enumerate(prompt.shots):
        opened = shot.description.count(DIALOGUE_OPEN)
        closed = shot.description.count(DIALOGUE_CLOSE)
        if opened or closed:
            defects.append(
                PromptDefect("delimiter_unbalanced", f"shots.{index}.description")
            )
        for line_index, line in enumerate(shot.dialogue):
            if DIALOGUE_OPEN in line.identity or DIALOGUE_CLOSE in line.identity:
                defects.append(
                    PromptDefect(
                        "delimiter_unbalanced",
                        f"shots.{index}.dialogue.{line_index}.identity",
                    )
                )
    return tuple(defects)


def _speaker_ids_are_continuous(
    prompt: StructuredPrompt,
) -> tuple[PromptDefect, ...]:
    """Require speaker ids to be assigned in vocalization order with no gaps."""
    assigned: list[int] = []
    defects: list[PromptDefect] = []
    for index, shot in enumerate(prompt.shots):
        for line_index, line in enumerate(shot.dialogue):
            for identifier in line.speaker_id.split(","):
                number = int(identifier[1:])
                if number in assigned:
                    continue
                if number != len(assigned) + 1:
                    defects.append(
                        PromptDefect(
                            "speaker_id_not_continuous",
                            f"shots.{index}.dialogue.{line_index}.speaker_id",
                        )
                    )
                assigned.append(number)
    return tuple(defects)


def _camera_values_are_in_vocabulary(
    prompt: StructuredPrompt,
) -> tuple[PromptDefect, ...]:
    """Require camera motion, amplitude, and speed to come from the tables."""
    defects: list[PromptDefect] = []
    for index, shot in enumerate(prompt.shots):
        camera = shot.camera
        if camera is None:
            continue
        if not CAMERA_MOTION_TYPES.permits(camera.motion):
            defects.append(
                PromptDefect("camera_motion_unknown", f"shots.{index}.camera.motion")
            )
        if camera.amplitude is not None and not CAMERA_AMPLITUDES.permits(
            camera.amplitude
        ):
            defects.append(
                PromptDefect(
                    "camera_amplitude_unknown", f"shots.{index}.camera.amplitude"
                )
            )
        if camera.speed is not None and not CAMERA_SPEEDS.permits(camera.speed):
            defects.append(
                PromptDefect("camera_speed_unknown", f"shots.{index}.camera.speed")
            )
    return tuple(defects)


def _cut_phrases_are_in_vocabulary(
    prompt: StructuredPrompt,
) -> tuple[PromptDefect, ...]:
    """Require a declared cut phrase to come from the pinned table."""
    return tuple(
        PromptDefect("cut_phrase_unknown", f"shots.{index}.cut_phrase")
        for index, shot in enumerate(prompt.shots)
        if shot.cut_phrase is not None and not CUT_PHRASES.permits(shot.cut_phrase)
    )


def _style_is_declared_once_on_the_opening_shot(
    prompt: StructuredPrompt,
) -> tuple[PromptDefect, ...]:
    """Require the style on the opening shot only, established once."""
    defects: list[PromptDefect] = []
    opening = prompt.shots[0]
    if opening.style is None:
        defects.append(PromptDefect("style_not_declared", "shots.0.style"))
    elif not VISUAL_STYLES.permits(opening.style):
        defects.append(PromptDefect("style_unknown", "shots.0.style"))
    defects.extend(
        PromptDefect("style_restated", f"shots.{index}.style")
        for index, shot in enumerate(prompt.shots)
        if index > 0 and shot.style is not None
    )
    return tuple(defects)


def _labels_resolve_to_a_definition(
    prompt: StructuredPrompt,
) -> tuple[PromptDefect, ...]:
    """Require every label used in a shot or retention entry to be defined."""
    defined = {entry.label for entry in prompt.subject_definitions}
    defects: list[PromptDefect] = []
    for index, shot in enumerate(prompt.shots):
        defects.extend(
            PromptDefect("reference_label_unresolved", f"shots.{index}.labels")
            for label in shot.labels
            if label not in defined
        )
    defects.extend(
        PromptDefect("reference_label_unresolved", f"retention_analysis.{index}.label")
        for index, entry in enumerate(prompt.retention_analysis)
        if entry.label not in defined
    )
    return tuple(defects)


def _every_definition_is_used(prompt: StructuredPrompt) -> tuple[PromptDefect, ...]:
    """Report a definition nobody references rather than carrying it silently."""
    used = {label for shot in prompt.shots for label in shot.labels}
    used.update(entry.label for entry in prompt.retention_analysis)
    return tuple(
        PromptDefect(
            "reference_definition_unused", f"subject_definitions.{index}.label"
        )
        for index, entry in enumerate(prompt.subject_definitions)
        if entry.label not in used
    )


def _retention_markers_are_in_vocabulary(
    prompt: StructuredPrompt,
) -> tuple[PromptDefect, ...]:
    """Require each label to draw its marker from the table for its kind."""
    defects: list[PromptDefect] = []
    for index, entry in enumerate(prompt.retention_analysis):
        table = (
            AUDIO_RETENTION_MARKERS
            if entry.label.startswith(AUDIO_LABEL_PREFIX)
            else VISUAL_RETENTION_MARKERS
        )
        if not table.permits(entry.marker):
            defects.append(
                PromptDefect(
                    "retention_marker_unknown", f"retention_analysis.{index}.marker"
                )
            )
    return tuple(defects)


def _speaker_ids_are_absent_from_retention(
    prompt: StructuredPrompt,
) -> tuple[PromptDefect, ...]:
    """Reject a speaker identifier in a retention record."""
    return tuple(
        PromptDefect("speaker_id_in_retention", f"retention_analysis.{index}.detail")
        for index, entry in enumerate(prompt.retention_analysis)
        if _mentions_speaker_id(entry.detail) or _mentions_speaker_id(entry.scope)
    )


def _task_types_are_in_vocabulary(
    prompt: StructuredPrompt,
) -> tuple[PromptDefect, ...]:
    """Require summary task types to come from the closed set without repeats."""
    summary = prompt.summary
    if summary is None:
        return (PromptDefect("summary_absent", "summary"),)
    defects = [
        PromptDefect("task_type_unknown", f"summary.task_types.{index}")
        for index, task_type in enumerate(summary.task_types)
        if not TASK_TYPES.permits(task_type)
    ]
    if len(set(summary.task_types)) != len(summary.task_types):
        defects.append(PromptDefect("task_type_repeated", "summary.task_types"))
    return tuple(defects)


def _mentions_speaker_id(text: str) -> bool:
    """Report whether prose carries a parenthesised speaker identifier."""
    for index, character in enumerate(text):
        if character != "(" or index + 2 >= len(text):
            continue
        if text[index + 1] == "S" and text[index + 2].isdigit():
            return True
    return False


CHECKS: Final[tuple[Check, ...]] = (
    Check("opening_shot_cut_time", ALL_MODES, _opening_shot_declares_no_cut),
    Check("shot_numbering_not_sequential", ALL_MODES, _shot_numbering_is_sequential),
    Check("cut_time_not_increasing", ALL_MODES, _cut_times_strictly_increase),
    Check("cut_time_outside_duration", ALL_MODES, _cut_times_are_inside_the_clip),
    Check("delimiter_unbalanced", ALL_MODES, _delimiters_are_balanced),
    Check("speaker_id_not_continuous", ALL_MODES, _speaker_ids_are_continuous),
    Check("camera_motion_unknown", ALL_MODES, _camera_values_are_in_vocabulary),
    Check("cut_phrase_unknown", ALL_MODES, _cut_phrases_are_in_vocabulary),
    Check("style_not_declared", ALL_MODES, _style_is_declared_once_on_the_opening_shot),
    Check(
        "reference_label_unresolved", REFERENCE_MODE, _labels_resolve_to_a_definition
    ),
    Check("reference_definition_unused", REFERENCE_MODE, _every_definition_is_used),
    Check(
        "retention_marker_unknown", REFERENCE_MODE, _retention_markers_are_in_vocabulary
    ),
    Check(
        "speaker_id_in_retention",
        REFERENCE_MODE,
        _speaker_ids_are_absent_from_retention,
    ),
    Check("task_type_unknown", REFERENCE_MODE, _task_types_are_in_vocabulary),
)


def validate_prompt(
    prompt: StructuredPrompt, checks: Sequence[Check] = CHECKS
) -> ValidationResult:
    """Validate a composed prompt and report what was and was not checked.

    Args:
        prompt: The composed prompt to check.
        checks: The check set to run. Injected so a test can drive validation
            with a population of one.

    Returns:
        The defects found, plus the rules run and the rules skipped.

    """
    defects: list[PromptDefect] = []
    run: list[str] = []
    skipped: list[SkippedCheck] = []

    for check in checks:
        if prompt.mode not in check.modes:
            skipped.append(
                SkippedCheck(
                    rule=check.rule,
                    reason=f"not applicable to {prompt.mode}",
                )
            )
            continue
        run.append(check.rule)
        defects.extend(check.run(prompt))

    return ValidationResult(
        defects=tuple(defects),
        checks_run=tuple(run),
        checks_skipped=tuple(skipped),
    )


def require_valid_prompt(
    prompt: StructuredPrompt, checks: Sequence[Check] = CHECKS
) -> ValidationResult:
    """Validate a prompt and reject it when any structural defect is found.

    Args:
        prompt: The composed prompt to check.
        checks: The check set to run.

    Returns:
        The accepted result, carrying the run and skipped inventories.

    Raises:
        PromptValidationError: The prompt carries at least one defect. The error
            names the first offending field and rule; the full defect list rides
            on :attr:`PromptValidationError.defects`.

    """
    result = validate_prompt(prompt, checks)
    if result.accepted:
        return result

    raise PromptValidationError(
        "prompt_structurally_invalid",
        "Prompt failed one or more structural checks.",
        defects=result.defects,
        context=result.defects[0].as_context(),
    )
