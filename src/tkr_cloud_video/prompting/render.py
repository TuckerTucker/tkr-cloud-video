"""Deterministic rendering of a validated prompt to H3 wire text.

Rendering is a pure function of the prompt and the pinned grammar: no clock, no
iteration over unordered containers, no locale-dependent formatting. Two renders
of one prompt are byte-identical, in this process and any other, which is what
makes the prompt digest a stable identity.
"""

from __future__ import annotations

from typing import Final

from tkr_cloud_video.prompting.alignment import alignment_header
from tkr_cloud_video.prompting.errors import PromptRenderError
from tkr_cloud_video.prompting.grammar import (
    CAMERA_MOTIONS,
    DIALOGUE_CLOSE,
    DIALOGUE_OPEN,
    VOICEOVER_PHRASE,
)
from tkr_cloud_video.prompting.models import DialogueLine, Shot, StructuredPrompt

SECTION_SEPARATOR: Final[str] = "\n\n"
_MOTION_PHRASES: Final[dict[str, str]] = {
    motion.label: motion.phrase for motion in CAMERA_MOTIONS
}


def format_cut_time(seconds: float) -> str:
    """Return a cut time as MM:SS.mmm, the notation the guide fixes."""
    total_milliseconds = round(seconds * 1000)
    minutes, remainder = divmod(total_milliseconds, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{minutes:02d}:{whole_seconds:02d}.{milliseconds:03d}"


def render_camera(shot: Shot) -> str:
    """Return the camera sentence for a shot, or an empty string.

    Amplitude and speed are emitted only when present: the grammar declares
    medium amplitude and normal speed as omitted defaults, so writing them out
    would state a direction the caller did not give.

    Raises:
        PromptRenderError: The motion has no prose form in the pinned table.

    """
    camera = shot.camera
    if camera is None:
        return ""
    phrase = _MOTION_PHRASES.get(camera.motion)
    if phrase is None:
        raise PromptRenderError(
            "unrenderable_vocabulary_value",
            "Camera motion has no wire form in the pinned grammar.",
            context={"field": "camera.motion", "rule": "camera_motion_type"},
        )
    modifiers = [value for value in (camera.amplitude, camera.speed) if value]
    return " ".join(["The camera", phrase, *modifiers]) + "."


def render_dialogue(line: DialogueLine) -> str:
    """Return one dialogue clause with caller text inside the delimiters."""
    speaker = f"({line.speaker_id})"
    lead = f"{line.identity} {speaker}".strip() if line.identity else speaker
    verb = VOICEOVER_PHRASE if line.voiceover else "says"
    spoken = f"{DIALOGUE_OPEN}[{line.language}] {line.text}{DIALOGUE_CLOSE}"
    clause = f"{lead} {verb}: {spoken}"
    if line.voiceover:
        clause = f"{clause} while their lips remain completely closed."
    if line.continues_across_cut:
        clause = f"{clause} The line continues seamlessly across the cut."
    if line.truncated_by_end:
        clause = f"{clause} The line is cut off by the end of the video."
    return clause


def render_on_screen_text(shot: Shot) -> str:
    """Return the visible-text clause, with caller text quoted verbatim."""
    if not shot.on_screen_text:
        return ""
    quoted = " ".join(f'"{value}"' for value in shot.on_screen_text)
    return f"On-screen text reads {quoted}."


def render_shot(shot: Shot, *, is_opening: bool) -> str:
    """Return one shot's wire text.

    The opening shot establishes the style and carries no timestamp; a later shot
    opens with its cut time and cut phrase.
    """
    parts: list[str] = [f"[Shot {shot.number}]"]
    if is_opening:
        if shot.style:
            parts.append(f"{shot.style},")
        parts.append(shot.description)
    else:
        if shot.cut_at_seconds is None:
            raise PromptRenderError(
                "unrenderable_vocabulary_value",
                "A later shot carries no cut time to render.",
                context={"field": "cut_at_seconds", "rule": "cut_time"},
            )
        parts.append(f"At {format_cut_time(shot.cut_at_seconds)},")
        if shot.cut_phrase:
            parts.append(f"{shot.cut_phrase}")
        parts.append(shot.description)

    for clause in (render_camera(shot), render_on_screen_text(shot)):
        if clause:
            parts.append(clause)
    parts.extend(render_dialogue(line) for line in shot.dialogue)
    return " ".join(parts)


def render_timeline(prompt: StructuredPrompt) -> str:
    """Return every shot in playback order as one continuous body."""
    return " ".join(
        render_shot(shot, is_opening=index == 0)
        for index, shot in enumerate(prompt.shots)
    )


def render_base_prompt(prompt: StructuredPrompt) -> str:
    """Return the three-field body used by the text and keyframe modes."""
    return SECTION_SEPARATOR.join(
        [
            f"integrated_multimodal_description: {render_timeline(prompt)}",
            f"overall_soundscape: {prompt.overall_soundscape}",
            f"non_diegetic_music: {prompt.non_diegetic_music}",
        ]
    )


def render_reference_prompt(prompt: StructuredPrompt) -> str:
    """Return the six-section body used by full-reference mode."""
    summary = prompt.summary
    if summary is None:
        raise PromptRenderError(
            "unrenderable_vocabulary_value",
            "A full-reference prompt carries no summary to render.",
            context={"field": "summary", "rule": "reference_sections"},
        )

    definitions = "\n".join(
        f"{entry.label} is {entry.definition}."
        if entry.speaker_id is None
        else f"{entry.label} ({entry.speaker_id}) is {entry.definition}."
        for entry in prompt.subject_definitions
    )
    prefix = " + ".join(summary.task_types)
    retention = "\n".join(
        f"{entry.label}{f' ({entry.scope})' if entry.scope else ''}: "
        f"{entry.marker} - {entry.detail}."
        for entry in prompt.retention_analysis
    )
    return SECTION_SEPARATOR.join(
        [
            f"subject_definitions:\n{definitions}",
            f"summary:\n[{prefix}] {summary.text}",
            f"retention_analysis:\n{retention}",
            f"detailed_description:\n{render_timeline(prompt)}",
            f"overall_soundscape:\n{prompt.overall_soundscape}",
            f"non_diegetic_music:\n{prompt.non_diegetic_music}",
        ]
    )


def render_prompt(prompt: StructuredPrompt) -> str:
    """Return the complete wire text for a validated prompt.

    Args:
        prompt: A prompt that has already passed structural validation.

    Returns:
        The exact text to submit, byte-identical across processes.

    Raises:
        PromptRenderError: A value in the prompt has no wire form.

    """
    body = (
        render_reference_prompt(prompt)
        if prompt.is_reference_mode
        else render_base_prompt(prompt)
    )
    header = alignment_header(prompt)
    return f"{header}{SECTION_SEPARATOR}{body}" if header else body
