"""The shipped prompt templates and their documentation must match the code.

A template is a promise about the request the service accepts, and a vocabulary
table in prose is a claim about a table in code. Neither is true because it was
written down, so both are checked here: the worked example is composed, validated
and rendered, and every vocabulary named in the document is compared against the
pinned grammar.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Final

import pytest

from tkr_cloud_video.prompt_authoring.composition import compose_prompt_authoring
from tkr_cloud_video.prompting.grammar import (
    ABSENT_VALUE,
    AUDIO_RETENTION_MARKERS,
    CAMERA_AMPLITUDES,
    CAMERA_MOTIONS,
    CAMERA_SPEEDS,
    CUT_PHRASES,
    GRAMMAR_REVISION,
    MODES,
    TASK_TYPES,
    UNINTELLIGIBLE_VALUE,
    VISUAL_RETENTION_MARKERS,
    VISUAL_STYLES,
)
from tkr_cloud_video.prompting.models import StructuredPrompt
from tkr_cloud_video.prompting.render import render_prompt

DOCS: Final[Path] = Path(__file__).resolve().parents[2] / "docs" / "prompting"
TEMPLATE_DOC: Final[Path] = DOCS / "h3-prompt-template.md"
TEMPLATES: Final[tuple[Path, ...]] = (
    DOCS / "templates" / "base-mode.json",
    DOCS / "templates" / "reference-mode.json",
)
FENCE = re.compile(r"```(json|text)\n(.*?)```", re.DOTALL)


def document() -> str:
    """Return the template documentation."""
    return TEMPLATE_DOC.read_text(encoding="utf-8")


def blocks(language: str) -> list[str]:
    """Return every fenced block of one language, in document order."""
    return [body for fence, body in FENCE.findall(document()) if fence == language]


def worked_example() -> dict[str, Any]:
    """Return the documented worked example payload."""
    for body in blocks("json"):
        if '"mode": "T2VA"' in body:
            parsed: dict[str, Any] = json.loads(body)
            return parsed
    raise AssertionError("the documentation carries no worked example")


def strip_help(value: Any) -> Any:
    """Return a template with its guidance keys removed, as a filler would."""
    if isinstance(value, dict):
        return {
            key: strip_help(item)
            for key, item in value.items()
            if not key.startswith("$comment")
        }
    if isinstance(value, list):
        return [strip_help(item) for item in value]
    return value


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda path: path.name)
def test_template_is_valid_json(path: Path) -> None:
    assert json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda path: path.name)
def test_template_slots_are_real_model_fields(path: Path) -> None:
    """A slot the model does not accept would be refused as an unknown field."""
    template = strip_help(json.loads(path.read_text(encoding="utf-8")))
    known = set(StructuredPrompt.model_fields)

    assert set(template) <= known, f"template names fields the model rejects: {path}"
    assert "shots" in template
    assert "overall_soundscape" in template
    assert "non_diegetic_music" in template


def test_every_template_slot_carries_a_placeholder_or_a_real_value() -> None:
    """No slot may be left as an empty string a filler would miss."""
    for path in TEMPLATES:
        template = strip_help(json.loads(path.read_text(encoding="utf-8")))
        assert "" not in json.dumps(template).split('": "')[1:], path.name


def test_worked_example_composes_validates_and_renders() -> None:
    prompt, result = compose_prompt_authoring().accept(worked_example())

    assert result.accepted
    assert prompt.grammar_revision == GRAMMAR_REVISION
    assert render_prompt(prompt)


def test_worked_example_renders_exactly_what_the_document_claims() -> None:
    """The documented output is the contract; a drifted renderer must fail here."""
    prompt, _ = compose_prompt_authoring().accept(worked_example())
    documented = next(
        body for body in blocks("text") if "integrated_multimodal_description:" in body
    )

    assert render_prompt(prompt) == documented.rstrip("\n")


def test_document_names_the_loaded_grammar_revision() -> None:
    assert GRAMMAR_REVISION in document()


@pytest.mark.parametrize(
    ("table", "label"),
    [
        (VISUAL_STYLES, "visual style"),
        (CUT_PHRASES, "cut phrase"),
        (TASK_TYPES, "task type"),
        (VISUAL_RETENTION_MARKERS, "visual retention marker"),
        (AUDIO_RETENTION_MARKERS, "audio retention marker"),
        (CAMERA_AMPLITUDES, "camera amplitude"),
        (CAMERA_SPEEDS, "camera speed"),
    ],
    ids=lambda value: getattr(value, "name", value),
)
def test_documented_vocabulary_covers_the_pinned_table(table: Any, label: str) -> None:
    """Every value in the code must appear in the prose that claims to list it."""
    text = document()

    missing = [value for value in table.values if value not in text]

    assert missing == [], f"{label} values absent from the document: {missing}"


def test_documented_camera_table_lists_every_motion_and_its_prose_form() -> None:
    text = document()

    missing = [
        motion.label
        for motion in CAMERA_MOTIONS
        if motion.label not in text or motion.phrase not in text
    ]

    assert missing == []


def test_document_states_the_omitted_defaults() -> None:
    text = document()

    assert CAMERA_AMPLITUDES.omitted_default is not None
    assert CAMERA_SPEEDS.omitted_default is not None
    assert CAMERA_AMPLITUDES.omitted_default in text
    assert CAMERA_SPEEDS.omitted_default in text


def test_document_states_the_absent_and_unclear_tokens() -> None:
    text = document()

    assert f"`{ABSENT_VALUE}`" in text
    assert f"`{UNINTELLIGIBLE_VALUE}`" in text


def test_document_lists_every_mode() -> None:
    text = document()

    assert [mode for mode in MODES if f"`{mode}`" not in text] == []
