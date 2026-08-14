"""Prompt digest and grammar pinning contracts."""

from __future__ import annotations

from typing import Any

import pytest

from tkr_cloud_video.prompt_authoring.composition import compose_prompt_authoring
from tkr_cloud_video.prompting.errors import PromptProvenanceError
from tkr_cloud_video.prompting.grammar import GRAMMAR_REVISION, grammar_fingerprint
from tkr_cloud_video.prompting.models import StructuredPrompt, compose_prompt
from tkr_cloud_video.prompting.provenance import (
    bind_provenance,
    digest_wire_text,
)
from tkr_cloud_video.prompting.render import render_prompt

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


def accepted(**overrides: Any) -> StructuredPrompt:
    """Return a prompt accepted through the composed boundary."""
    prompt, _ = compose_prompt_authoring().accept(payload(**overrides))
    return prompt


def test_provenance_records_digest_revision_and_grammar_digest() -> None:
    provenance = bind_provenance(accepted())

    assert len(provenance.prompt_digest) == 64
    assert provenance.grammar_revision == GRAMMAR_REVISION
    assert provenance.grammar_digest == grammar_fingerprint()


def test_digest_matches_the_rendered_text() -> None:
    prompt = accepted()

    provenance = bind_provenance(prompt)

    assert provenance.prompt_digest == digest_wire_text(render_prompt(prompt))


def test_identical_prompts_share_a_digest() -> None:
    assert bind_provenance(accepted()).prompt_digest == (
        bind_provenance(accepted()).prompt_digest
    )


def test_a_changed_prompt_changes_the_digest() -> None:
    first = bind_provenance(accepted()).prompt_digest
    second = bind_provenance(accepted(non_diegetic_music="Sparse piano.")).prompt_digest

    assert first != second


def test_metadata_carries_only_digests_and_a_revision() -> None:
    metadata = bind_provenance(accepted()).as_metadata()

    assert set(metadata) == {
        "prompt_digest",
        "prompt_grammar_revision",
        "prompt_grammar_digest",
    }
    joined = " ".join(metadata.values())
    assert "baker" not in joined
    assert "shutters" not in joined


def test_metadata_omits_caller_dialogue() -> None:
    line = "First batch of the morning."
    prompt = accepted(
        shots=[
            {
                **OPENING,
                "dialogue": [{"speaker_id": "S1", "language": "English", "text": line}],
            }
        ]
    )

    metadata = bind_provenance(prompt).as_metadata()

    assert line not in " ".join(metadata.values())


def test_a_prompt_with_no_revision_is_refused() -> None:
    prompt = compose_prompt(payload())

    with pytest.raises(PromptProvenanceError) as raised:
        bind_provenance(prompt)

    assert raised.value.code == "grammar_revision_unbound"


def test_a_prompt_from_another_revision_is_refused() -> None:
    prompt = compose_prompt(payload()).model_copy(
        update={"grammar_revision": "h3-1999-01"}
    )

    with pytest.raises(PromptProvenanceError) as raised:
        bind_provenance(prompt)

    assert raised.value.code == "grammar_revision_unbound"
    assert raised.value.context["rule"] == GRAMMAR_REVISION


def test_a_non_reproducible_renderer_is_refused() -> None:
    """A digest is an identity only if the text it covers is reproducible."""
    calls: list[int] = []

    def drifting(prompt: StructuredPrompt) -> str:
        calls.append(1)
        return f"{render_prompt(prompt)} call={len(calls)}"

    with pytest.raises(PromptProvenanceError) as raised:
        bind_provenance(accepted(), renderer=drifting)

    assert raised.value.code == "digest_source_not_canonical"


def test_empty_text_is_not_digested() -> None:
    with pytest.raises(PromptProvenanceError) as raised:
        digest_wire_text("")

    assert raised.value.code == "digest_source_not_canonical"
