"""Deterministic rendering contracts."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

import pytest

from tkr_cloud_video.prompting.errors import PromptRenderError
from tkr_cloud_video.prompting.models import compose_prompt
from tkr_cloud_video.prompting.render import (
    format_cut_time,
    render_camera,
    render_prompt,
)

OPENING: dict[str, Any] = {
    "number": 1,
    "style": "Cinematic",
    "description": "A medium-wide shot frames a baker opening the shutters.",
}


def payload(**overrides: Any) -> dict[str, Any]:
    """Return a valid base-mode payload."""
    document: dict[str, Any] = {
        "mode": "T2VA",
        "duration_seconds": 8.0,
        "shots": [dict(OPENING)],
        "overall_soundscape": "Wooden shutters scrape open over a quiet street.",
        "non_diegetic_music": "N/A",
    }
    document.update(overrides)
    return document


def reference_payload(**overrides: Any) -> dict[str, Any]:
    """Return a valid full-reference payload."""
    document: dict[str, Any] = {
        "mode": "Ref2VA",
        "duration_seconds": 6.0,
        "subject_definitions": [
            {"label": "<Subject 1>", "definition": "the young woman in <Picture 1>"},
            {
                "label": "<Audio 1>",
                "definition": "the voice-timbre reference",
                "speaker_id": "S1",
            },
        ],
        "summary": {
            "task_types": ["reference generation", "audio reference"],
            "text": "The target video shows <Subject 1> in a coffee shop.",
        },
        "retention_analysis": [
            {
                "label": "<Subject 1>",
                "marker": "fully_preserved",
                "scope": "appears in [Shot 1]",
                "detail": "her identity and light-pink shirt are retained",
            },
            {
                "label": "<Audio 1>",
                "marker": "reference",
                "detail": "its timbre guides delivery without copying the signal",
            },
        ],
        "shots": [{**OPENING, "labels": ["<Subject 1>", "<Audio 1>"]}],
        "overall_soundscape": "Soft indoor coffee-shop room tone continues.",
        "non_diegetic_music": "N/A",
    }
    document.update(overrides)
    return document


def test_base_mode_emits_the_three_fields_in_order() -> None:
    text = render_prompt(compose_prompt(payload()))

    assert text.index("integrated_multimodal_description:") == 0
    assert text.index("overall_soundscape:") < text.index("non_diegetic_music:")
    assert "[Shot 1] Cinematic," in text


def test_reference_mode_emits_six_sections_in_order() -> None:
    text = render_prompt(compose_prompt(reference_payload()))

    positions = [
        text.index(name)
        for name in (
            "subject_definitions:",
            "summary:",
            "retention_analysis:",
            "detailed_description:",
            "overall_soundscape:",
            "non_diegetic_music:",
        )
    ]

    assert positions == sorted(positions)
    assert "[reference generation + audio reference]" in text
    assert "<Audio 1> (S1) is the voice-timbre reference." in text
    assert "<Subject 1> (appears in [Shot 1]): fully_preserved -" in text


def test_keyframe_header_precedes_the_body_with_a_blank_line() -> None:
    text = render_prompt(compose_prompt(payload(mode="I2VA")))

    header, blank, rest = text.partition("\n\n")

    assert header.startswith("For the target video")
    assert blank == "\n\n"
    assert rest.startswith("integrated_multimodal_description:")


def test_rendering_is_byte_identical_within_the_process() -> None:
    prompt = compose_prompt(reference_payload())

    assert render_prompt(prompt) == render_prompt(prompt)


def test_rendering_is_byte_identical_across_processes() -> None:
    """A digest identifies a prompt only if the text is stable between runs."""
    document = reference_payload()
    program = (
        "import json,sys;"
        "from tkr_cloud_video.prompting.models import compose_prompt;"
        "from tkr_cloud_video.prompting.render import render_prompt;"
        "sys.stdout.write(render_prompt(compose_prompt(json.loads(sys.argv[1]))))"
    )

    runs = [
        subprocess.run(  # noqa: S603 - fixed argv, no shell.
            [sys.executable, "-c", program, json.dumps(document)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for _ in range(2)
    ]

    assert runs[0] == runs[1]
    assert runs[0] == render_prompt(compose_prompt(document))


def test_default_camera_modifiers_are_omitted() -> None:
    document = payload(shots=[{**OPENING, "camera": {"motion": "Push In"}}])

    text = render_prompt(compose_prompt(document))

    assert "The camera pushes in." in text
    assert "amplitude" not in text
    assert "speed" not in text


def test_declared_camera_modifiers_are_emitted_in_order() -> None:
    document = payload(
        shots=[
            {
                **OPENING,
                "camera": {
                    "motion": "Truck Right",
                    "amplitude": "with small amplitude",
                    "speed": "at slow speed",
                },
            }
        ]
    )

    text = render_prompt(compose_prompt(document))

    assert "The camera trucks right with small amplitude at slow speed." in text


def test_absent_music_emits_the_grammar_token() -> None:
    text = render_prompt(compose_prompt(payload(non_diegetic_music="N/A")))

    assert "non_diegetic_music: N/A" in text


def test_cut_time_uses_the_fixed_notation() -> None:
    document = payload(
        shots=[
            dict(OPENING),
            {
                "number": 2,
                "cut_at_seconds": 3.5,
                "cut_phrase": "the camera cuts to",
                "description": "a close-up of steam rising",
            },
        ]
    )

    text = render_prompt(compose_prompt(document))

    assert "[Shot 2] At 00:03.500, the camera cuts to" in text


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0.0, "00:00.000"),
        (3.5, "00:03.500"),
        (65.25, "01:05.250"),
        (9.999, "00:09.999"),
    ],
)
def test_format_cut_time_cases(seconds: float, expected: str) -> None:
    assert format_cut_time(seconds) == expected


def test_dialogue_places_only_language_and_text_inside_the_delimiters() -> None:
    document = payload(
        shots=[
            {
                **OPENING,
                "dialogue": [
                    {
                        "speaker_id": "S1",
                        "identity": "The middle-aged baker with a raspy voice",
                        "language": "English",
                        "text": "First batch of the morning.",
                    }
                ],
            }
        ]
    )

    text = render_prompt(compose_prompt(document))

    assert (
        "The middle-aged baker with a raspy voice (S1) says: "
        "<d>[English] First batch of the morning.</d>" in text
    )


def test_voiceover_states_the_closed_lips() -> None:
    document = payload(
        shots=[
            {
                **OPENING,
                "dialogue": [
                    {
                        "speaker_id": "S1",
                        "language": "English",
                        "text": "I still remember that road.",
                        "voiceover": True,
                    }
                ],
            }
        ]
    )

    text = render_prompt(compose_prompt(document))

    assert "says in an off-screen voiceover:" in text
    assert "lips remain completely closed" in text


def test_on_screen_text_is_quoted_and_untranslated() -> None:
    document = payload(shots=[{**OPENING, "on_screen_text": ["营业中"]}])

    text = render_prompt(compose_prompt(document))

    assert 'On-screen text reads "营业中".' in text


def test_motion_without_a_wire_form_is_refused() -> None:
    prompt = compose_prompt(
        payload(shots=[{**OPENING, "camera": {"motion": "Dolly Sideways"}}])
    )

    with pytest.raises(PromptRenderError) as raised:
        render_camera(prompt.shots[0])

    assert raised.value.code == "unrenderable_vocabulary_value"


def test_later_shot_without_a_cut_time_is_refused() -> None:
    prompt = compose_prompt(
        payload(shots=[dict(OPENING), {"number": 2, "description": "a close-up"}])
    )

    with pytest.raises(PromptRenderError):
        render_prompt(prompt)
