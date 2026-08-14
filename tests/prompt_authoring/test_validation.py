"""Structural validator contracts."""

from __future__ import annotations

from typing import Any

import pytest

from tkr_cloud_video.prompting.errors import PromptValidationError
from tkr_cloud_video.prompting.models import compose_prompt
from tkr_cloud_video.prompting.validation import (
    CHECKS,
    Check,
    require_valid_prompt,
    validate_prompt,
)

OPENING: dict[str, Any] = {
    "number": 1,
    "style": "Cinematic",
    "description": "A medium-wide shot frames a baker opening the shutters.",
}


def payload(**overrides: Any) -> dict[str, Any]:
    """Return a structurally valid base-mode payload."""
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
    """Return a structurally valid full-reference payload."""
    document: dict[str, Any] = {
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
        "shots": [{**OPENING, "labels": ["<Subject 1>"]}],
        "overall_soundscape": "Soft indoor coffee-shop room tone continues.",
        "non_diegetic_music": "N/A",
    }
    document.update(overrides)
    return document


def rules(document: dict[str, Any]) -> set[str]:
    """Return the rules reported for a payload."""
    result = validate_prompt(compose_prompt(document))
    return {defect.rule for defect in result.defects}


def test_a_valid_base_prompt_is_accepted() -> None:
    result = validate_prompt(compose_prompt(payload()))

    assert result.accepted
    assert result.defects == ()


def test_a_valid_reference_prompt_is_accepted() -> None:
    assert validate_prompt(compose_prompt(reference_payload())).accepted


def test_cut_past_the_end_of_the_clip_is_rejected() -> None:
    document = payload(
        duration_seconds=6.0,
        shots=[dict(OPENING), {"number": 2, "cut_at_seconds": 7.0, "description": "x"}],
    )

    assert "cut_time_outside_duration" in rules(document)


def test_cut_times_must_strictly_increase() -> None:
    document = payload(
        shots=[
            dict(OPENING),
            {"number": 2, "cut_at_seconds": 3.0, "description": "a"},
            {"number": 3, "cut_at_seconds": 3.0, "description": "b"},
        ]
    )

    assert "cut_time_not_increasing" in rules(document)


def test_opening_shot_may_not_carry_a_cut_time() -> None:
    document = payload(shots=[{**OPENING, "cut_at_seconds": 0.0}])

    assert "opening_shot_cut_time" in rules(document)


def test_later_shot_must_carry_a_cut_time() -> None:
    document = payload(shots=[dict(OPENING), {"number": 2, "description": "x"}])

    assert "later_shot_missing_cut_time" in rules(document)


def test_shot_numbers_must_be_sequential() -> None:
    document = payload(
        shots=[dict(OPENING), {"number": 4, "cut_at_seconds": 3.0, "description": "x"}]
    )

    assert "shot_numbering_not_sequential" in rules(document)


def test_service_prose_may_not_carry_a_dialogue_delimiter() -> None:
    document = payload(shots=[{**OPENING, "description": "she says <d>hello</d>"}])

    assert "delimiter_unbalanced" in rules(document)


def test_speaker_ids_must_not_skip() -> None:
    document = payload(
        shots=[
            {
                **OPENING,
                "dialogue": [
                    {"speaker_id": "S2", "language": "English", "text": "Hello."}
                ],
            }
        ]
    )

    assert "speaker_id_not_continuous" in rules(document)


def test_camera_motion_outside_the_table_is_rejected() -> None:
    document = payload(shots=[{**OPENING, "camera": {"motion": "Dolly Sideways"}}])

    assert "camera_motion_unknown" in rules(document)


def test_omitted_camera_default_is_not_a_defect() -> None:
    document = payload(
        shots=[
            {
                **OPENING,
                "camera": {"motion": "Push In", "amplitude": None, "speed": None},
            }
        ]
    )

    assert validate_prompt(compose_prompt(document)).accepted


def test_camera_amplitude_default_label_is_not_renderable() -> None:
    document = payload(
        shots=[{**OPENING, "camera": {"motion": "Push In", "amplitude": "medium"}}]
    )

    assert "camera_amplitude_unknown" in rules(document)


def test_cut_phrase_outside_the_table_is_rejected() -> None:
    document = payload(
        shots=[
            dict(OPENING),
            {
                "number": 2,
                "cut_at_seconds": 3.0,
                "cut_phrase": "we smash cut to",
                "description": "x",
            },
        ]
    )

    assert "cut_phrase_unknown" in rules(document)


def test_style_must_be_declared_on_the_opening_shot() -> None:
    document = payload(shots=[{"number": 1, "description": "a shot"}])

    assert "style_not_declared" in rules(document)


def test_style_may_not_be_restated_on_a_later_shot() -> None:
    document = payload(
        shots=[
            dict(OPENING),
            {
                "number": 2,
                "cut_at_seconds": 3.0,
                "style": "Cinematic",
                "description": "x",
            },
        ]
    )

    assert "style_restated" in rules(document)


def test_dangling_reference_label_is_rejected() -> None:
    document = reference_payload(
        shots=[{**OPENING, "labels": ["<Subject 1>", "<Subject 2>"]}]
    )

    assert "reference_label_unresolved" in rules(document)


def test_unused_definition_is_reported() -> None:
    document = reference_payload(
        subject_definitions=[
            {"label": "<Subject 1>", "definition": "the young woman"},
            {"label": "<Subject 2>", "definition": "a Samoyed nobody films"},
        ]
    )

    assert "reference_definition_unused" in rules(document)


def test_audio_label_draws_from_the_audio_marker_table() -> None:
    document = reference_payload(
        subject_definitions=[
            {"label": "<Audio 1>", "definition": "the voice-timbre reference"}
        ],
        retention_analysis=[
            {
                "label": "<Audio 1>",
                "marker": "fully_preserved",
                "detail": "reused as the final audio track",
            }
        ],
        shots=[{**OPENING, "labels": ["<Audio 1>"]}],
    )

    assert "retention_marker_unknown" in rules(document)


def test_speaker_identifier_is_refused_in_retention() -> None:
    document = reference_payload(
        retention_analysis=[
            {
                "label": "<Subject 1>",
                "marker": "fully_preserved",
                "detail": "the woman (S1) keeps her light-pink shirt",
            }
        ]
    )

    assert "speaker_id_in_retention" in rules(document)


def test_task_type_outside_the_closed_set_is_rejected() -> None:
    document = reference_payload(
        summary={"task_types": ["vibe transfer"], "text": "The target video."}
    )

    assert "task_type_unknown" in rules(document)


def test_repeated_task_type_is_rejected() -> None:
    document = reference_payload(
        summary={
            "task_types": ["audio reuse", "audio reuse"],
            "text": "The target video.",
        }
    )

    assert "task_type_repeated" in rules(document)


def test_a_passing_result_enumerates_the_checks_it_did_not_run() -> None:
    """A validator that cannot name its gaps cannot be trusted when it is green."""
    result = validate_prompt(compose_prompt(payload()))

    assert result.accepted
    assert result.checks_skipped
    skipped = {check.rule for check in result.checks_skipped}
    assert "reference_label_unresolved" in skipped
    assert all("not applicable to T2VA" in c.reason for c in result.checks_skipped)
    assert skipped.isdisjoint(result.checks_run)


def test_reference_mode_runs_every_check() -> None:
    result = validate_prompt(compose_prompt(reference_payload()))

    assert result.checks_skipped == ()
    assert len(result.checks_run) == len(CHECKS)


def test_check_set_is_injectable_for_a_population_of_one() -> None:
    only = [check for check in CHECKS if check.rule == "shot_numbering_not_sequential"]
    document = payload(
        shots=[dict(OPENING), {"number": 9, "cut_at_seconds": 3.0, "description": "x"}]
    )

    result = validate_prompt(compose_prompt(document), checks=only)

    assert result.checks_run == ("shot_numbering_not_sequential",)
    assert [defect.rule for defect in result.defects] == [
        "shot_numbering_not_sequential"
    ]


def test_require_valid_prompt_raises_with_every_defect() -> None:
    document = payload(
        duration_seconds=6.0,
        shots=[
            {**OPENING, "cut_at_seconds": 0.0},
            {"number": 5, "cut_at_seconds": 7.0, "description": "x"},
        ],
    )

    with pytest.raises(PromptValidationError) as raised:
        require_valid_prompt(compose_prompt(document))

    error = raised.value
    assert error.code == "prompt_structurally_invalid"
    assert len(error.defects) > 1
    assert {"rule", "field"} == set(error.context)
    assert "defects" in error.to_safe_dict()


def test_a_rejection_carries_no_prompt_text() -> None:
    caller_line = "I get off at the next station."
    document = payload(
        shots=[
            {
                **OPENING,
                "dialogue": [
                    {"speaker_id": "S3", "language": "English", "text": caller_line}
                ],
            }
        ]
    )

    with pytest.raises(PromptValidationError) as raised:
        require_valid_prompt(compose_prompt(document))

    serialized = str(raised.value.to_safe_dict())
    assert caller_line not in serialized
    assert "baker" not in serialized


def test_require_valid_prompt_returns_the_inventory_on_success() -> None:
    result = require_valid_prompt(compose_prompt(payload()))

    assert result.accepted
    assert result.checks_run


def test_every_check_declares_at_least_one_mode() -> None:
    assert all(isinstance(check, Check) and check.modes for check in CHECKS)
