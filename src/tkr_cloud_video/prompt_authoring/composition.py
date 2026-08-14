"""IoC composition for structured prompt composition and validation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from tkr_cloud_video.prompting.errors import PromptGrammarIntegrityError
from tkr_cloud_video.prompting.grammar import (
    GRAMMAR_DIGEST,
    GRAMMAR_REVISION,
    grammar_fingerprint,
    verify_grammar_integrity,
)
from tkr_cloud_video.prompting.models import StructuredPrompt, compose_prompt
from tkr_cloud_video.prompting.validation import (
    CHECKS,
    Check,
    ValidationResult,
    require_valid_prompt,
)


@dataclass(frozen=True, slots=True)
class PromptGrammarBinding:
    """The grammar revision a composed prompt was validated against."""

    revision: str
    digest: str


@dataclass(frozen=True, slots=True)
class PromptDependencies:
    """Policy configuration required by the prompt boundary.

    Args:
        expected_grammar_digest: Digest the loaded tables must reproduce.
        checks: Structural check set, injected so a caller can narrow it.

    """

    expected_grammar_digest: str = GRAMMAR_DIGEST
    checks: Sequence[Check] = field(default=CHECKS)


@dataclass(frozen=True, slots=True)
class PromptServices:
    """The composed prompt boundary."""

    binding: PromptGrammarBinding
    checks: Sequence[Check]

    def accept(
        self, payload: Mapping[str, Any]
    ) -> tuple[StructuredPrompt, ValidationResult]:
        """Compose and validate an external prompt payload.

        Args:
            payload: The external prompt document.

        Returns:
            The accepted prompt, bound to this service's grammar revision, and
            the validation result carrying the run and skipped inventories.

        Raises:
            PromptCompositionError: The payload's sections do not match its mode.
            PromptValidationError: The composed prompt is structurally invalid.

        """
        prompt = compose_prompt(payload)
        result = require_valid_prompt(prompt, self.checks)
        return prompt.model_copy(
            update={"grammar_revision": self.binding.revision}
        ), result


def compose_prompt_authoring(
    dependencies: PromptDependencies | None = None,
) -> PromptServices:
    """Compose the prompt boundary, failing closed on grammar drift.

    Args:
        dependencies: Explicit policy configuration. Defaults to the pinned
            grammar digest and the full check set.

    Returns:
        The composed services, bound to the verified grammar revision.

    Raises:
        PromptGrammarIntegrityError: The loaded tables do not reproduce the
            expected digest, so no prompt may be composed under this revision.

    """
    resolved = dependencies or PromptDependencies()
    verify_grammar_integrity(resolved.expected_grammar_digest)
    return PromptServices(
        binding=PromptGrammarBinding(
            revision=GRAMMAR_REVISION, digest=grammar_fingerprint()
        ),
        checks=resolved.checks,
    )


def grammar_report(expected: str = GRAMMAR_DIGEST) -> dict[str, object]:
    """Return the grammar's diagnostic report for the offline doctor check.

    The report is credential-free and makes no network request: it states the
    pinned revision and whether the loaded tables still reproduce its digest.

    Args:
        expected: The digest the tables must reproduce. Injected only so the
            drift branch can be driven by a test without editing a table.

    """
    try:
        verify_grammar_integrity(expected)
    except PromptGrammarIntegrityError:
        return {
            "revision": GRAMMAR_REVISION,
            "digest_verified": False,
            "observed_digest": grammar_fingerprint(),
        }
    return {
        "revision": GRAMMAR_REVISION,
        "digest_verified": True,
        "observed_digest": expected,
    }
