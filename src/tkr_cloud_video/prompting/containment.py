"""Containment of caller-supplied text at the render boundary.

Caller text is data, never grammar. Dialogue, lyrics, and on-screen text are
carried into the wire text byte for byte, and a span that would act as structure
is refused rather than escaped: escaping would silently alter words the guide
requires to be preserved verbatim, so rejection is the only option that keeps
both promises.

The rules police only the caller-controlled fields. Service-authored description
may legitimately carry grammar, and the structural validator polices that
separately.
"""

from __future__ import annotations

import re
from typing import Final

from tkr_cloud_video.prompting.errors import PromptContainmentError
from tkr_cloud_video.prompting.grammar import (
    CUTOFF_MARKER,
    DIALOGUE_CLOSE,
    DIALOGUE_OPEN,
    REFERENCE_SECTIONS,
    SCENE_TRANSITION_MARKER,
)
from tkr_cloud_video.prompting.models import StructuredPrompt

FORBIDDEN_MARKERS: Final[tuple[str, ...]] = (
    DIALOGUE_OPEN,
    DIALOGUE_CLOSE,
    SCENE_TRANSITION_MARKER,
    CUTOFF_MARKER,
)
SHOT_HEADER_PATTERN: Final[re.Pattern[str]] = re.compile(r"\[Shot\s*\d+\]")
REFERENCE_LABEL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"<(?:Subject|Picture|Video|Audio)\s*\d+>"
)
SECTION_NAMES: Final[frozenset[str]] = frozenset(
    {
        "integrated_multimodal_description",
        *REFERENCE_SECTIONS,
    }
)
CONTROL_CHARACTERS: Final[re.Pattern[str]] = re.compile(r"[\x00-\x1f\x7f]")


def _reject(field: str, rule: str) -> None:
    """Raise a containment error naming the field and the rule, never the text."""
    raise PromptContainmentError(
        "caller_span_carries_grammar",
        "Caller-supplied text would act as grammar in the rendered prompt.",
        context={"field": field, "rule": rule},
    )


def contain(field: str, value: str) -> str:
    """Return caller text unchanged, or refuse it as structure.

    Args:
        field: Dotted path used in diagnostics.
        value: The caller-supplied span.

    Returns:
        The value, unchanged, when it carries no structure.

    Raises:
        PromptContainmentError: The span carries a delimiter, a reference label,
            a shot header, a section opener, or a control character.

    """
    for marker in FORBIDDEN_MARKERS:
        if marker in value:
            _reject(field, "grammar_marker")
    if REFERENCE_LABEL_PATTERN.search(value):
        _reject(field, "reference_label")
    if SHOT_HEADER_PATTERN.search(value):
        _reject(field, "shot_header")
    if CONTROL_CHARACTERS.search(value):
        _reject(field, "control_character")
    if any(f"{name}:" in value for name in SECTION_NAMES):
        _reject(field, "section_opener")
    return value


def caller_spans(prompt: StructuredPrompt) -> tuple[tuple[str, str], ...]:
    """Return every caller-controlled span with its field path.

    These are the only fields a caller supplies: dialogue text and on-screen
    text. Everything else in the prompt is authored by this service.
    """
    spans: list[tuple[str, str]] = []
    for index, shot in enumerate(prompt.shots):
        for line_index, line in enumerate(shot.dialogue):
            spans.append((f"shots.{index}.dialogue.{line_index}.text", line.text))
        spans.extend(
            (f"shots.{index}.on_screen_text.{text_index}", value)
            for text_index, value in enumerate(shot.on_screen_text)
        )
    return tuple(spans)


def contain_prompt(prompt: StructuredPrompt) -> None:
    """Refuse a prompt whose caller text would act as grammar.

    Raises:
        PromptContainmentError: Any caller span carries structure.

    """
    for field, value in caller_spans(prompt):
        contain(field, value)


def require_verbatim(text: str, prompt: StructuredPrompt) -> None:
    """Verify every caller span survived rendering byte for byte.

    Containment guarantees a span may not act as structure; this guarantees the
    renderer did not alter it. Together they are the whole promise: the caller's
    words appear exactly once, exactly as given.

    Raises:
        PromptContainmentError: A caller span is absent from the rendered text,
            which means rendering changed it.

    """
    for field, value in caller_spans(prompt):
        if value not in text:
            raise PromptContainmentError(
                "caller_span_altered_by_render",
                "Caller-supplied text did not survive rendering unchanged.",
                context={"field": field, "rule": "verbatim_preservation"},
            )
