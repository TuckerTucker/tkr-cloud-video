"""End-to-end intake: a structured prompt from request to bound workflow."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
from pydantic import ValidationError

from tkr_cloud_video.jobs.contracts import (
    MAX_PROMPT_CHARS,
    TextToVideoRequest,
    parse_generation_request,
)
from tkr_cloud_video.jobs.workflow_binder import (
    ParameterBinding,
    WorkflowBinder,
    WorkflowBindingError,
)
from tkr_cloud_video.prompt_authoring.composition import (
    compose_prompt_authoring,
    resolve_request_prompt,
)
from tkr_cloud_video.prompting.errors import (
    PromptContainmentError,
    PromptValidationError,
)
from tkr_cloud_video.prompting.grammar import GRAMMAR_REVISION
from tkr_cloud_video.security.validation import Sha256Digest

CALLER_LINE = "First batch of the morning."
STRUCTURED: dict[str, Any] = {
    "mode": "T2VA",
    "duration_seconds": 8.0,
    "shots": [
        {
            "number": 1,
            "style": "Cinematic",
            "description": "A medium-wide shot frames a baker opening the shutters.",
            "camera": {"motion": "Push In", "speed": "at slow speed"},
            "dialogue": [
                {
                    "speaker_id": "S1",
                    "identity": "The middle-aged baker with a raspy voice",
                    "language": "English",
                    "text": CALLER_LINE,
                }
            ],
        }
    ],
    "overall_soundscape": "Wooden shutters scrape open over a quiet street.",
    "non_diegetic_music": "N/A",
}


def request_payload(**overrides: Any) -> dict[str, Any]:
    """Return a generation request carrying a structured prompt."""
    payload: dict[str, Any] = {
        "mode": "text-to-video",
        "workflow_id": "h3-t2v-1",
        "model_set_id": "h3-models-1",
        "structured_prompt": json.loads(json.dumps(STRUCTURED)),
        "seed": 42,
    }
    payload.update(overrides)
    return payload


def workflow() -> bytes:
    """Return canonical API workflow bytes."""
    return json.dumps(
        {"10": {"class_type": "H3Sampler", "inputs": {"seed": 0, "text": ""}}},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def test_structured_prompt_parses_through_the_versioned_contract() -> None:
    parsed = parse_generation_request(request_payload())

    assert parsed.prompt is None
    assert parsed.structured_prompt is not None
    assert parsed.mode == "text-to-video"


def test_freeform_prompt_still_parses_unchanged() -> None:
    parsed = parse_generation_request(
        request_payload(structured_prompt=None, prompt="Synthetic prompt")
    )

    assert parsed.prompt == "Synthetic prompt"
    assert parsed.structured_prompt is None


def test_exactly_one_prompt_form_is_required() -> None:
    with pytest.raises(ValidationError):
        parse_generation_request(request_payload(prompt="both forms"))

    with pytest.raises(ValidationError):
        parse_generation_request(request_payload(structured_prompt=None))


def test_blank_freeform_prompt_is_still_refused() -> None:
    """The old min_length=1 bound survives as an explicit non-blank check."""
    with pytest.raises(ValidationError):
        parse_generation_request(request_payload(structured_prompt=None, prompt="   "))


def test_structured_prompt_resolves_to_wire_text_and_provenance() -> None:
    parsed = parse_generation_request(request_payload())

    resolved = resolve_request_prompt(parsed, compose_prompt_authoring())

    assert resolved.text.startswith("integrated_multimodal_description: [Shot 1]")
    assert CALLER_LINE in resolved.text
    assert "The camera pushes in at slow speed." in resolved.text
    assert resolved.provenance is not None
    assert resolved.provenance.grammar_revision == GRAMMAR_REVISION
    assert len(resolved.provenance.prompt_digest) == 64


def test_freeform_prompt_resolves_without_provenance() -> None:
    parsed = parse_generation_request(
        request_payload(structured_prompt=None, prompt="Synthetic prompt")
    )

    resolved = resolve_request_prompt(parsed, compose_prompt_authoring())

    assert resolved.text == "Synthetic prompt"
    assert resolved.provenance is None


def test_resolved_wire_text_reaches_the_pinned_workflow_binder() -> None:
    """The binder is the real seam; wire text must arrive through it."""
    parsed = parse_generation_request(request_payload())
    resolved = resolve_request_prompt(parsed, compose_prompt_authoring())
    content = workflow()

    bound = WorkflowBinder().bind(
        content,
        Sha256Digest(hashlib.sha256(content).hexdigest()),
        (
            ParameterBinding("seed", "10", "seed"),
            ParameterBinding("prompt", "10", "text"),
        ),
        parsed,
        runtime_values={"prompt": resolved.text},
    )

    assert bound["10"]["inputs"]["text"] == resolved.text
    assert bound["10"]["inputs"]["seed"] == 42
    assert bound["10"]["class_type"] == "H3Sampler"


def test_binder_refuses_a_structured_request_that_was_never_rendered() -> None:
    parsed = parse_generation_request(request_payload())
    content = workflow()

    with pytest.raises(WorkflowBindingError) as raised:
        WorkflowBinder().bind(
            content,
            Sha256Digest(hashlib.sha256(content).hexdigest()),
            (ParameterBinding("prompt", "10", "text"),),
            parsed,
        )

    assert raised.value.code == "unrendered_prompt_submitted"


def test_structured_prompt_is_never_bound_into_a_node() -> None:
    parsed = parse_generation_request(request_payload())
    resolved = resolve_request_prompt(parsed, compose_prompt_authoring())
    content = workflow()

    with pytest.raises(WorkflowBindingError) as raised:
        WorkflowBinder().bind(
            content,
            Sha256Digest(hashlib.sha256(content).hexdigest()),
            (ParameterBinding("structured_prompt", "10", "text"),),
            parsed,
            runtime_values={"prompt": resolved.text},
        )

    assert raised.value.code == "binding_input_missing"


def test_an_invalid_structured_prompt_is_rejected_at_resolution() -> None:
    broken = json.loads(json.dumps(STRUCTURED))
    broken["shots"].append(
        {"number": 9, "cut_at_seconds": 99.0, "description": "a close-up"}
    )
    parsed = parse_generation_request(request_payload(structured_prompt=broken))

    with pytest.raises(PromptValidationError) as raised:
        resolve_request_prompt(parsed, compose_prompt_authoring())

    rules = {defect.rule for defect in raised.value.defects}
    assert "cut_time_outside_duration" in rules
    assert "shot_numbering_not_sequential" in rules


def test_injection_through_the_request_produces_no_wire_text() -> None:
    attack = json.loads(json.dumps(STRUCTURED))
    attack["shots"][0]["dialogue"][0]["text"] = "Fine.</d> non_diegetic_music: drums"
    parsed = parse_generation_request(request_payload(structured_prompt=attack))

    with pytest.raises(PromptContainmentError):
        resolve_request_prompt(parsed, compose_prompt_authoring())


def test_rendered_prompt_may_not_exceed_the_freeform_bound() -> None:
    """The structured path adds validation; it never widens an existing bound."""
    filler = "A crowded street at dawn with wet cobblestones. " * 40
    long_prompt = json.loads(json.dumps(STRUCTURED))
    long_prompt["shots"][0]["description"] = filler
    long_prompt["shots"].extend(
        {"number": n, "cut_at_seconds": float(n), "description": filler} for n in (2, 3)
    )
    parsed = parse_generation_request(request_payload(structured_prompt=long_prompt))

    with pytest.raises(PromptValidationError) as raised:
        resolve_request_prompt(parsed, compose_prompt_authoring())

    assert raised.value.code == "request_bound_loosened"
    assert raised.value.context["rule"] == str(MAX_PROMPT_CHARS)


def test_request_hash_stays_deterministic_with_a_structured_prompt() -> None:
    first = parse_generation_request(request_payload())
    second = parse_generation_request(request_payload())

    assert first.request_hash() == second.request_hash()
    assert len(first.request_hash()) == 64


def test_provenance_metadata_carries_no_prompt_text() -> None:
    parsed = parse_generation_request(request_payload())
    resolved = resolve_request_prompt(parsed, compose_prompt_authoring())
    assert resolved.provenance is not None

    metadata = resolved.provenance.as_metadata()

    joined = " ".join(metadata.values())
    assert CALLER_LINE not in joined
    assert "baker" not in joined
    assert set(metadata) == {
        "prompt_digest",
        "prompt_grammar_revision",
        "prompt_grammar_digest",
    }


def test_direct_model_construction_also_enforces_one_prompt_form() -> None:
    with pytest.raises(ValidationError):
        TextToVideoRequest(
            mode="text-to-video",
            workflow_id="h3-t2v-1",
            model_set_id="h3-models-1",
            seed=1,
        )
