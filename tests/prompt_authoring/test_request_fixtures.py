"""The committed request fixtures must still be submittable.

Every file in ``requests/`` is a request someone is expected to paste into a
console and run. Nothing else in the repository reads them, so until now the only
thing standing between a drifted fixture and a caller was that caller: a frame
count knocked off the model's temporal grid, or a shot naming a camera motion the
grammar no longer carries, would sit green in the tree and fail at submission -
after a cold start. `docs/audits/2026-08-15-generation-latency.md` measures that
overhead at about seven minutes, which is what a defect here costs to discover.

The checks bind to the handler's own path rather than restating it.
:func:`parse_generation_request` already enforces the canvas multiple, the canvas
ceiling, the temporal grid and the frame range from the trained envelope of the
model set each fixture names, and :func:`resolve_request_prompt` composes,
validates, renders and bounds a structured prompt exactly as intake does. Calling
both is therefore the whole contract; asserting those bounds again here would
only pin a second copy of numbers that live in
:mod:`tkr_cloud_video.jobs.trained_envelope`.

What is deliberately not asserted is where a fixture sits *inside* that envelope.
Four of the committed fixtures run below the trained frame floor on purpose -
that is the cheap smoke test the envelope reports rather than refuses - so a test
requiring trained-range membership would reject the population it exists to
protect.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import pytest

from tkr_cloud_video.jobs.contracts import GenerationRequest, parse_generation_request
from tkr_cloud_video.prompt_authoring.composition import (
    compose_prompt_authoring,
    resolve_request_prompt,
)

REQUESTS: Final[Path] = Path(__file__).resolve().parents[2] / "requests"
FIXTURES: Final[tuple[Path, ...]] = tuple(sorted(REQUESTS.glob("*.json")))


def payload(path: Path) -> dict[str, Any]:
    """Return one fixture's job envelope.

    Args:
        path: The committed request file.

    Returns:
        The ``input`` document, which is what a console submits.

    """
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return dict(document["input"])


def parsed(path: Path) -> GenerationRequest:
    """Return one fixture parsed through the contract the handler parses with."""
    return parse_generation_request(payload(path))


def test_the_fixture_population_is_not_empty() -> None:
    """A glob that matches nothing would pass every check below silently."""
    assert FIXTURES, f"no request fixtures were discovered under {REQUESTS}"


@pytest.mark.parametrize("path", FIXTURES, ids=lambda path: path.name)
def test_fixture_is_a_job_envelope(path: Path) -> None:
    """A console submits the whole envelope, not the bare request."""
    document = json.loads(path.read_text(encoding="utf-8"))

    assert set(document) == {"input"}, f"{path.name} is not a job envelope"


@pytest.mark.parametrize("path", FIXTURES, ids=lambda path: path.name)
def test_fixture_parses_through_the_generation_contract(path: Path) -> None:
    """This is the canvas, temporal-grid and frame-range check, once."""
    assert parsed(path).request_hash()


@pytest.mark.parametrize("path", FIXTURES, ids=lambda path: path.name)
def test_fixture_resolves_to_wire_text(path: Path) -> None:
    """A structured fixture must compose, validate, render and stay in bounds."""
    resolved = resolve_request_prompt(parsed(path), compose_prompt_authoring())

    assert resolved.text.strip()


def test_the_structured_prompt_path_is_exercised_by_the_population() -> None:
    """Report a population that has stopped covering the structured form.

    Every check above passes for a set of freeform fixtures without composing a
    single structured prompt, so the interesting half of the contract could go
    unexercised while the file still reads as green.
    """
    structured = [
        path.name
        for path in FIXTURES
        if payload(path).get("structured_prompt") is not None
    ]

    assert structured, "no committed fixture carries a structured prompt"
