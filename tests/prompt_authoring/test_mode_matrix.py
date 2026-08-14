"""Per-mode section contracts and discrimination proofs for the named checks.

The discrimination tests here answer a question the per-rule tests cannot: does
each named check actually cause the rejection attributed to it? Each payload is
validated twice - once with the full check set, once with that single rule
removed - so a check that never fired would be caught rather than credited.
"""

from __future__ import annotations

from typing import Any

import pytest

from tkr_cloud_video.prompting.alignment import alignment_header
from tkr_cloud_video.prompting.grammar import (
    BASE_SECTIONS,
    MODES,
    REFERENCE_SECTIONS,
    SECTION_ORDER,
)
from tkr_cloud_video.prompting.models import compose_prompt
from tkr_cloud_video.prompting.validation import CHECKS, validate_prompt

OPENING: dict[str, Any] = {
    "number": 1,
    "style": "Cinematic",
    "description": "A medium-wide shot frames a baker opening the shutters.",
}
SOUNDSCAPE = "Wooden shutters scrape open over a quiet street."


def base(**overrides: Any) -> dict[str, Any]:
    """Return a valid base-mode payload."""
    document: dict[str, Any] = {
        "mode": "T2VA",
        "duration_seconds": 8.0,
        "shots": [dict(OPENING)],
        "overall_soundscape": SOUNDSCAPE,
        "non_diegetic_music": "N/A",
    }
    document.update(overrides)
    return document


def reference(**overrides: Any) -> dict[str, Any]:
    """Return a valid full-reference payload."""
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
        "overall_soundscape": SOUNDSCAPE,
        "non_diegetic_music": "N/A",
    }
    document.update(overrides)
    return document


@pytest.mark.parametrize("mode", MODES)
def test_every_mode_composes_and_validates(mode: str) -> None:
    document = reference() if mode == "Ref2VA" else base(mode=mode)

    prompt = compose_prompt(document)

    assert prompt.mode == mode
    assert validate_prompt(prompt).accepted


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (mode, REFERENCE_SECTIONS if mode == "Ref2VA" else BASE_SECTIONS)
        for mode in MODES
    ],
)
def test_section_order_is_fixed_per_mode(mode: str, expected: tuple[str, ...]) -> None:
    document = reference() if mode == "Ref2VA" else base(mode=mode)

    assert compose_prompt(document).section_order == expected
    assert SECTION_ORDER[mode] == expected


@pytest.mark.parametrize(
    ("mode", "expects_header"),
    [
        ("T2VA", False),
        ("I2VA", True),
        ("FL2VA", True),
        ("L2VA", True),
        ("Ref2VA", False),
    ],
)
def test_only_keyframe_modes_carry_an_alignment_header(
    mode: str, expects_header: bool
) -> None:
    document = reference() if mode == "Ref2VA" else base(mode=mode)

    header = alignment_header(compose_prompt(document))

    assert bool(header) is expects_header


@pytest.mark.parametrize(
    ("mode", "duration", "expected_mark"),
    [("FL2VA", 8.0, "8.00"), ("FL2VA", 6.5, "6.50"), ("L2VA", 12.0, "12.00")],
)
def test_alignment_timing_is_stated_to_two_decimals(
    mode: str, duration: float, expected_mark: str
) -> None:
    prompt = compose_prompt(base(mode=mode, duration_seconds=duration))

    assert f"{expected_mark}-second mark" in alignment_header(prompt)


def without(rule: str) -> list[Any]:
    """Return the check set with one rule removed."""
    return [check for check in CHECKS if check.rule != rule]


DISCRIMINATION_CASES: list[tuple[str, dict[str, Any]]] = [
    (
        "cut_time_outside_duration",
        base(
            duration_seconds=6.0,
            shots=[
                dict(OPENING),
                {"number": 2, "cut_at_seconds": 7.0, "description": "a close-up"},
            ],
        ),
    ),
    (
        "delimiter_unbalanced",
        base(shots=[{**OPENING, "description": "she says <d>hello</d> quietly"}]),
    ),
    (
        "reference_label_unresolved",
        reference(shots=[{**OPENING, "labels": ["<Subject 1>", "<Subject 4>"]}]),
    ),
    (
        "reference_definition_unused",
        reference(
            subject_definitions=[
                {"label": "<Subject 1>", "definition": "the young woman"},
                {"label": "<Subject 2>", "definition": "a dog nobody films"},
            ]
        ),
    ),
]


@pytest.mark.parametrize(
    ("rule", "document"), DISCRIMINATION_CASES, ids=[c[0] for c in DISCRIMINATION_CASES]
)
def test_named_check_is_the_reason_for_the_rejection(
    rule: str, document: dict[str, Any]
) -> None:
    prompt = compose_prompt(document)

    with_rule = validate_prompt(prompt)
    without_rule = validate_prompt(prompt, checks=without(rule))

    assert rule in {defect.rule for defect in with_rule.defects}
    assert rule not in {defect.rule for defect in without_rule.defects}
    assert without_rule.accepted, "the payload must isolate exactly one rule"


def test_skipped_inventory_and_run_inventory_together_cover_every_check() -> None:
    """No check may vanish: run plus skipped must account for the whole set."""
    for mode in MODES:
        document = reference() if mode == "Ref2VA" else base(mode=mode)
        result = validate_prompt(compose_prompt(document))

        accounted = set(result.checks_run) | {c.rule for c in result.checks_skipped}
        assert accounted == {check.rule for check in CHECKS}
