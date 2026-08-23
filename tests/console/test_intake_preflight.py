"""The console refuses locally exactly what the worker would refuse remotely."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tkr_cloud_video.console.intake import (
    Accepted,
    Rejection,
    form_constraints,
    preflight,
    structured_example,
)
from tkr_cloud_video.jobs.contracts import GenerationRequest
from tkr_cloud_video.jobs.trained_envelope import TRAINED_RANGES
from tkr_cloud_video.prompt_authoring.composition import compose_prompt_authoring
from tkr_cloud_video.release.runpod_handler import HandlerResponse, RunPodHandler

ROOT = Path(__file__).resolve().parents[2]
MODEL_SET = "minimax-h3-t2v-int8-20260809"


def base(**overrides: Any) -> dict[str, Any]:
    """Return a minimal valid text-to-video request document."""
    document: dict[str, Any] = {
        "schema_version": "1",
        "mode": "text-to-video",
        "workflow_id": "minimax-h3-t2v",
        "model_set_id": MODEL_SET,
        "prompt": "A calm wide shot of a snow-covered pine forest at dawn.",
        "seed": 7,
    }
    document.update(overrides)
    return document


@pytest.fixture(name="prompts")
def prompts_fixture() -> Any:
    """Compose the prompt boundary once per test."""
    return compose_prompt_authoring()


def test_absent_dimensions_are_filled_from_the_named_model_set(prompts: Any) -> None:
    """A request naming no canvas lands on its own model's trained numbers."""
    accepted = preflight(base(), prompts)

    assert isinstance(accepted, Accepted)
    entry = TRAINED_RANGES[MODEL_SET]
    assert accepted.request.width == entry.default_width
    assert accepted.request.height == entry.default_height
    assert accepted.request.frames == entry.default_frames
    assert accepted.below_trained_envelope is False


def test_a_short_clip_is_permitted_and_reported_rather_than_refused(
    prompts: Any,
) -> None:
    """Below the trained floor stays legal and stops being silent."""
    accepted = preflight(base(frames=73), prompts)

    assert isinstance(accepted, Accepted)
    assert accepted.below_trained_envelope is True
    payload = accepted.as_payload()
    assert payload["trained_envelope"] == {
        "inside": False,
        "short_edge_below_trained": False,
        "frames_below_trained": True,
        "revision": "h3-2026-08",
    }


@pytest.mark.parametrize(
    "document",
    [
        base(frames=74),
        base(width=1376),
        base(width=810),
        base(model_set_id="minimax-h3-t2v-int8-19990101"),
        base(prompt=None),
        base(structured_prompt={"mode": "T2VA"}),
        {"mode": "text-to-video"},
    ],
    ids=[
        "frames-off-grid",
        "canvas-over-ceiling",
        "canvas-off-multiple",
        "unregistered-model-set",
        "no-prompt-form",
        "both-forms-absent-and-structured-incomplete",
        "not-a-request",
    ],
)
def test_a_defective_request_is_refused_before_submission(
    document: dict[str, Any], prompts: Any
) -> None:
    """Every refusal names a code, so nothing reaches the endpoint unnamed."""
    rejected = preflight(document, prompts)

    assert isinstance(rejected, Rejection)
    assert rejected.error_code
    assert rejected.as_payload()["ok"] is False


def test_the_wire_text_of_a_freeform_prompt_is_the_prompt(prompts: Any) -> None:
    """A freeform prompt passes through as the text that reaches the workflow."""
    accepted = preflight(base(prompt="Exactly this."), prompts)

    assert isinstance(accepted, Accepted)
    assert accepted.wire_text == "Exactly this."


def test_a_structured_prompt_is_rendered_rather_than_supplied(prompts: Any) -> None:
    """The console shows back text it derived, not the document it was handed.

    This is the whole reason the wire text is returned at all: a caller that
    submits a structured prompt has otherwise no way to see what it composed to,
    and the text is what the model is actually given.
    """
    document = json.loads(
        (ROOT / "requests" / "structured-prompt-native-canvas.json").read_text()
    )

    accepted = preflight(document["input"], prompts)

    assert isinstance(accepted, Accepted)
    assert accepted.request.structured_prompt is not None
    assert accepted.wire_text
    assert accepted.wire_text != json.dumps(accepted.request.structured_prompt)
    assert len(accepted.wire_text) <= 4000


def test_a_structured_defect_is_reported_by_field_and_rule(prompts: Any) -> None:
    """A prompt defect names its field, so a form can mark the input at fault."""
    document = json.loads(
        (ROOT / "requests" / "structured-prompt-native-canvas.json").read_text()
    )
    payload = dict(document["input"])
    broken = dict(payload["structured_prompt"])
    broken["shots"] = [
        {**dict(broken["shots"][0]), "camera": {"motion": "Barrel Roll"}},
        *broken["shots"][1:],
    ]

    rejected = preflight({**payload, "structured_prompt": broken}, prompts)

    assert isinstance(rejected, Rejection)
    assert rejected.defects
    assert all(defect["field"] and defect["rule"] for defect in rejected.defects)


class _UnreachableApplication:
    """An application the preflight agreement test must never reach."""

    async def submit(
        self, request: GenerationRequest
    ) -> tuple[str, str | None]:  # pragma: no cover - never called
        raise AssertionError("validation must not submit")


@pytest.mark.parametrize(
    "document",
    [
        base(),
        base(frames=73),
        base(frames=74),
        base(width=1376),
        base(model_set_id="minimax-h3-t2v-int8-19990101"),
        base(prompt=None),
        base(prompt=None, structured_prompt={"mode": "T2VA"}),
        {"not": "a request"},
    ],
)
def test_console_preflight_agrees_with_the_handler_it_stands_in_for(
    document: dict[str, Any], prompts: Any
) -> None:
    """The console's local refusal is the worker's refusal, decision for decision.

    The console reproduces the handler's two validation steps rather than
    importing it, because the handler needs an application it must not be given
    here. That makes agreement a thing to pin, and this is where it is pinned: a
    change to either side that makes them disagree fails on this test rather
    than on a request the console said was fine and the endpoint refused.
    """
    handler = RunPodHandler(_UnreachableApplication(), prompts)

    remote = handler.validate({"input": document})
    local = preflight(document, prompts)

    if isinstance(remote, HandlerResponse):
        assert isinstance(local, Rejection)
        assert local.error_code == remote.error_code
    else:
        assert isinstance(local, Accepted)
        assert local.request == remote


def test_form_constraints_are_served_from_the_registry_not_restated() -> None:
    """The form's bounds come from the pinned envelope and grammar."""
    constraints = form_constraints()

    assert constraints["envelope_revision"] == "h3-2026-08"
    assert constraints["max_prompt_chars"] == 4000
    served = {entry["model_set_id"] for entry in constraints["model_sets"]}
    assert served == set(TRAINED_RANGES)
    entry = next(
        item for item in constraints["model_sets"] if item["model_set_id"] == MODEL_SET
    )
    registered = TRAINED_RANGES[MODEL_SET]
    assert entry["canvas_multiple"] == registered.canvas_multiple
    assert entry["grid_stride"] == registered.grid_stride
    assert entry["max_frames"] == registered.max_frames
    assert constraints["vocabularies"]


def test_a_refusal_names_the_rule_the_request_broke(prompts: Any) -> None:
    """The discriminator tag is bookkeeping; the message is the diagnostic.

    A frame count off the temporal grid is the likeliest mistake a caller makes,
    and pydantic locates it at the union's discriminator rather than at
    ``frames``. Without the message a caller is told only that some value error
    occurred somewhere in a request it can already see.
    """
    rejected = preflight(base(frames=74), prompts)

    assert isinstance(rejected, Rejection)
    assert rejected.defects == (
        {
            "field": "request",
            "rule": "value_error",
            "message": "frames must satisfy the model set's 17k+5 grid",
        },
    )


def test_a_defect_never_carries_the_value_that_caused_it(prompts: Any) -> None:
    """A prompt field's offending value is the prompt, so no defect may hold it.

    Pydantic keeps the input under its own key rather than interpolating it into
    the message, and this is where that stays true: a bound broken by a 6,000
    character prompt reports the bound, not the six thousand characters.
    """
    caller_text = "CALLER-SUPPLIED-PROMPT-TEXT"
    rejected = preflight(base(prompt=caller_text * 400), prompts)

    assert isinstance(rejected, Rejection)
    assert caller_text not in json.dumps(rejected.as_payload())
    assert rejected.defects == (
        {
            "field": "prompt",
            "rule": "string_too_long",
            "message": "String should have at most 4000 characters",
        },
    )


def test_a_prompt_defect_and_a_contract_defect_share_one_shape(prompts: Any) -> None:
    """The page renders both kinds of refusal with one code path."""
    document = json.loads(
        (ROOT / "requests" / "structured-prompt-native-canvas.json").read_text()
    )
    payload = dict(document["input"])
    broken = dict(payload["structured_prompt"])
    broken["shots"] = [
        {**dict(broken["shots"][0]), "camera": {"motion": "Barrel Roll"}},
        *broken["shots"][1:],
    ]

    from_prompt = preflight({**payload, "structured_prompt": broken}, prompts)
    from_contract = preflight(base(frames=74), prompts)

    assert isinstance(from_prompt, Rejection)
    assert isinstance(from_contract, Rejection)
    keys = {"field", "rule", "message"}
    assert all(set(defect) == keys for defect in from_prompt.defects)
    assert all(set(defect) == keys for defect in from_contract.defects)


def test_the_structured_example_is_the_request_it_came_from(prompts: Any) -> None:
    """The form opens on a document that validates, and cannot drift from it.

    The example is packaged so it ships with the wheel rather than being read
    out of a checkout, which is exactly what lets it drift from the request file
    it was taken from. This is where that is caught.
    """
    source = json.loads(
        (ROOT / "requests" / "structured-prompt-native-canvas.json").read_text()
    )

    example = structured_example()

    assert example == source["input"]["structured_prompt"]
    assert isinstance(
        preflight({**base(prompt=None), "structured_prompt": example}, prompts),
        Accepted,
    )
    assert form_constraints()["structured_example"] == example
