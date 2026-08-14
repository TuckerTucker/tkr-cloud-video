"""Prompt identity: a stable digest bound to the grammar that produced it.

A committed result must be able to say which prompt produced it and which
vocabulary that prompt was built from, without carrying the prompt body. Two
fields answer both: the digest of the canonical wire text, and the grammar
revision plus its table digest.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from tkr_cloud_video.prompting.errors import PromptProvenanceError
from tkr_cloud_video.prompting.grammar import (
    GRAMMAR_REVISION,
    grammar_fingerprint,
)
from tkr_cloud_video.prompting.models import StructuredPrompt
from tkr_cloud_video.prompting.render import render_prompt

DIGEST_LENGTH: Final[int] = 64
Renderer = Callable[[StructuredPrompt], str]


@dataclass(frozen=True, slots=True)
class PromptProvenance:
    """The three scalars a committed result records about its prompt.

    Args:
        prompt_digest: SHA-256 of the canonical wire text.
        grammar_revision: The pinned revision the prompt validated against.
        grammar_digest: SHA-256 of the grammar tables for that revision.

    """

    prompt_digest: str
    grammar_revision: str
    grammar_digest: str

    def as_metadata(self) -> dict[str, str]:
        """Return the fields to merge into reproducibility metadata.

        The prompt body, caller dialogue, and on-screen text are absent by
        construction: only digests and a revision identifier are returned.
        """
        return {
            "prompt_digest": self.prompt_digest,
            "prompt_grammar_revision": self.grammar_revision,
            "prompt_grammar_digest": self.grammar_digest,
        }


def digest_wire_text(text: str) -> str:
    """Return the SHA-256 digest of already-canonical wire text.

    Raises:
        PromptProvenanceError: The text is empty, so it cannot identify a prompt.

    """
    if not text:
        raise PromptProvenanceError(
            "digest_source_not_canonical",
            "Refusing to digest empty prompt text.",
            context={"field": "wire_text"},
        )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def bind_provenance(
    prompt: StructuredPrompt,
    *,
    renderer: Renderer = render_prompt,
) -> PromptProvenance:
    """Render a prompt canonically and bind its identity to the grammar.

    Args:
        prompt: A validated prompt carrying the revision it was accepted under.
        renderer: The canonical renderer. Injected so a test can prove that a
            non-canonical renderer is refused rather than trusted.

    Returns:
        The prompt digest and the grammar revision and digest.

    Raises:
        PromptProvenanceError: The prompt carries no grammar revision, its
            revision is not the one loaded, or the rendered text is not
            reproducible under the same renderer.

    """
    revision = prompt.grammar_revision
    if revision is None:
        raise PromptProvenanceError(
            "grammar_revision_unbound",
            "Prompt carries no grammar revision to record.",
            context={"field": "grammar_revision"},
        )
    if revision != GRAMMAR_REVISION:
        raise PromptProvenanceError(
            "grammar_revision_unbound",
            "Prompt was accepted under a revision this process has not loaded.",
            context={"field": "grammar_revision", "rule": GRAMMAR_REVISION},
        )

    text = renderer(prompt)
    if renderer(prompt) != text:
        raise PromptProvenanceError(
            "digest_source_not_canonical",
            "Renderer is not reproducible, so its digest cannot be an identity.",
            context={"field": "wire_text"},
        )

    return PromptProvenance(
        prompt_digest=digest_wire_text(text),
        grammar_revision=revision,
        grammar_digest=grammar_fingerprint(),
    )
