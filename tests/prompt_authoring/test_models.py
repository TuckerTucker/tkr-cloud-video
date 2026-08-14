"""Structured prompt composition and keyframe alignment contracts."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from tkr_cloud_video.prompting.alignment import (
    PromptAlignmentError,
    alignment_header,
    format_mark,
)
from tkr_cloud_video.prompting.models import (
    PromptCompositionError,
    StructuredPrompt,
    absent_or,
    compose_prompt,
    speaker_ids,
)

BASE_SHOT: dict[str, Any] = {
    "number": 1,
    "description": "A medium-wide shot frames a baker opening the shutters.",
}


def base_payload(**overrides: Any) -> dict[str, Any]:
    """Return a minimal valid base-mode payload in the order the mode fixes."""
    payload: dict[str, Any] = {
        "mode": "T2VA",
        "duration_seconds": 8.0,
        "shots": [dict(BASE_SHOT)],
        "overall_soundscape": "Wooden shutters scrape open over a quiet street.",
        "non_diegetic_music": "N/A",
    }
    payload.update(overrides)
    return payload


def reference_payload(**overrides: Any) -> dict[str, Any]:
    """Return a minimal valid full-reference payload in fixed section order."""
    payload: dict[str, Any] = {
        "mode": "Ref2VA",
        "duration_seconds": 6.0,
        "subject_definitions": [
            {"label": "<Subject 1>", "definition": "the young woman in <Picture 1>"}
        ],
        "summary": {
            "task_types": ["reference generation"],
            "text": "The target video shows <Subject 1> in a coffee shop.",
        },
        "retention_analysis": [
            {
                "label": "<Subject 1>",
                "marker": "fully_preserved",
                "detail": "her identity and light-pink shirt are retained",
            }
        ],
        "shots": [dict(BASE_SHOT)],
        "overall_soundscape": "Soft indoor coffee-shop room tone continues.",
        "non_diegetic_music": "N/A",
    }
    payload.update(overrides)
    return payload


def test_base_mode_payload_composes() -> None:
    prompt = compose_prompt(base_payload())

    assert isinstance(prompt, StructuredPrompt)
    assert prompt.mode == "T2VA"
    assert prompt.section_order[0] == "integrated_multimodal_description"
    assert not prompt.is_reference_mode


def test_reference_mode_payload_composes() -> None:
    prompt = compose_prompt(reference_payload())

    assert prompt.is_reference_mode
    assert prompt.section_order[0] == "subject_definitions"
    assert prompt.summary is not None
    assert prompt.summary.task_types == ("reference generation",)


def test_unknown_mode_is_rejected() -> None:
    with pytest.raises(PromptCompositionError) as raised:
        compose_prompt(base_payload(mode="T2V"))

    assert raised.value.code == "prompt_mode_unknown"


def test_absent_section_is_rejected_rather_than_defaulted() -> None:
    payload = base_payload()
    del payload["non_diegetic_music"]

    with pytest.raises(PromptCompositionError) as raised:
        compose_prompt(payload)

    assert raised.value.code == "section_set_mismatch_for_mode"
    assert "non_diegetic_music" in str(raised.value.context["field"])


def test_reference_only_section_is_rejected_in_a_base_mode() -> None:
    payload = base_payload(
        summary={"task_types": ["reference generation"], "text": "x"}
    )

    with pytest.raises(PromptCompositionError) as raised:
        compose_prompt(payload)

    assert raised.value.code == "section_set_mismatch_for_mode"
    assert "summary" in str(raised.value.context["field"])


def test_reordered_reference_sections_are_not_silently_accepted() -> None:
    ordered = reference_payload()
    reordered = {
        key: ordered[key]
        for key in [
            "mode",
            "duration_seconds",
            "summary",
            "subject_definitions",
            "retention_analysis",
            "shots",
            "overall_soundscape",
            "non_diegetic_music",
        ]
    }

    with pytest.raises(PromptCompositionError) as raised:
        compose_prompt(reordered)

    assert raised.value.code == "section_set_mismatch_for_mode"


def test_unknown_field_is_refused_at_the_boundary() -> None:
    with pytest.raises(ValidationError):
        compose_prompt(base_payload(director="someone"))


def test_composed_prompt_is_immutable() -> None:
    prompt = compose_prompt(base_payload())

    with pytest.raises(ValidationError):
        prompt.duration_seconds = 9.0  # type: ignore[misc]


@pytest.mark.parametrize(
    ("mode", "expected_start"),
    [
        ("T2VA", ""),
        ("I2VA", "For the target video, at 0.00 seconds"),
        ("FL2VA", "How the reference pictures align"),
        ("L2VA", "How the reference pictures align"),
    ],
)
def test_alignment_header_per_mode(mode: str, expected_start: str) -> None:
    prompt = compose_prompt(base_payload(mode=mode))
    header = alignment_header(prompt)

    assert header.startswith(expected_start)
    if not expected_start:
        assert header == ""


def test_closing_keyframe_is_stated_to_two_decimals() -> None:
    prompt = compose_prompt(base_payload(mode="FL2VA", duration_seconds=8.0))

    assert "8.00-second mark" in alignment_header(prompt)


def test_reference_mode_anchors_no_keyframe_header() -> None:
    assert alignment_header(compose_prompt(reference_payload())) == ""


def test_duration_that_two_decimals_cannot_state_is_rejected() -> None:
    prompt = compose_prompt(base_payload(mode="L2VA", duration_seconds=8.005))

    with pytest.raises(PromptAlignmentError) as raised:
        alignment_header(prompt)

    assert raised.value.code == "alignment_time_not_expressible"


def test_closing_keyframe_must_land_inside_its_own_shot() -> None:
    payload = base_payload(
        mode="L2VA",
        duration_seconds=6.0,
        shots=[
            dict(BASE_SHOT),
            {"number": 2, "cut_at_seconds": 6.0, "description": "a close-up"},
        ],
    )

    with pytest.raises(PromptAlignmentError) as raised:
        alignment_header(compose_prompt(payload))

    assert raised.value.code == "alignment_time_outside_duration"


def test_format_mark_pads_to_two_decimals() -> None:
    assert format_mark(0) == "0.00"
    assert format_mark(8.5) == "8.50"


def test_absent_or_returns_the_grammar_token() -> None:
    assert absent_or(None) == "N/A"
    assert absent_or("") == "N/A"
    assert absent_or("sparse piano") == "sparse piano"


def test_speaker_ids_follow_first_vocalization_order() -> None:
    payload = base_payload(
        shots=[
            {
                **BASE_SHOT,
                "dialogue": [
                    {"speaker_id": "S2", "language": "English", "text": "After you."},
                    {"speaker_id": "S1,S2", "language": "English", "text": "Wait!"},
                ],
            }
        ]
    )

    assert speaker_ids(compose_prompt(payload).shots) == ("S2", "S1")
