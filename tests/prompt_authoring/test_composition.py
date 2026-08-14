"""Prompt boundary composition contracts."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

import pytest

from tkr_cloud_video.prompt_authoring.composition import (
    PromptDependencies,
    compose_prompt_authoring,
    grammar_report,
)
from tkr_cloud_video.prompting.errors import (
    PromptGrammarIntegrityError,
    PromptValidationError,
)
from tkr_cloud_video.prompting.grammar import GRAMMAR_REVISION
from tkr_cloud_video.prompting.validation import CHECKS

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


def test_composition_binds_the_verified_grammar_revision() -> None:
    services = compose_prompt_authoring()

    assert services.binding.revision == GRAMMAR_REVISION
    assert len(services.binding.digest) == 64


def test_composition_fails_closed_on_grammar_drift() -> None:
    with pytest.raises(PromptGrammarIntegrityError) as raised:
        compose_prompt_authoring(PromptDependencies(expected_grammar_digest="0" * 64))

    assert raised.value.code == "grammar_digest_mismatch"


def test_accepted_prompt_carries_the_bound_revision() -> None:
    services = compose_prompt_authoring()

    prompt, result = services.accept(payload())

    assert prompt.grammar_revision == GRAMMAR_REVISION
    assert result.accepted
    assert result.checks_run


def test_boundary_rejects_an_invalid_prompt() -> None:
    services = compose_prompt_authoring()

    with pytest.raises(PromptValidationError):
        services.accept(payload(shots=[{"number": 3, "description": "a shot"}]))


def test_check_set_narrows_through_dependencies() -> None:
    only = [c for c in CHECKS if c.rule == "shot_numbering_not_sequential"]
    services = compose_prompt_authoring(PromptDependencies(checks=only))

    _, result = services.accept(payload(shots=[{"number": 1, "description": "a shot"}]))

    assert result.checks_run == ("shot_numbering_not_sequential",)


def test_grammar_report_is_offline_and_verified() -> None:
    report = grammar_report()

    assert report == {
        "revision": GRAMMAR_REVISION,
        "digest_verified": True,
        "observed_digest": report["observed_digest"],
    }


def test_doctor_reports_the_grammar_through_the_real_cli() -> None:
    """The diagnostic entrypoint is the surface an operator actually drives."""
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell.
        [sys.executable, "-m", "tkr_cloud_video", "doctor", "--json"],
        capture_output=True,
        text=True,
        check=True,
    )

    result = json.loads(completed.stdout)

    assert result["outcome"] == "succeeded"
    assert result["prompt_grammar"]["digest_verified"] is True
    assert result["prompt_grammar"]["revision"] == GRAMMAR_REVISION


def test_grammar_report_states_drift_rather_than_raising() -> None:
    report = grammar_report(expected="0" * 64)

    assert report["digest_verified"] is False
    assert report["observed_digest"] != "0" * 64
    assert report["revision"] == GRAMMAR_REVISION
