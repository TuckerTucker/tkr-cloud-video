"""Typed prompt errors and the field-scoped defects they carry.

Every error here is constructed with allowlisted context only. The prompt body,
caller dialogue, lyrics, and on-screen text never appear in an error: a defect
names the field and the rule it broke, and the caller reads its own payload for
the offending text.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from tkr_cloud_video.core.errors import AppError


@dataclass(frozen=True, slots=True)
class PromptDefect:
    """One structural defect, named by the field and the rule it broke.

    Args:
        rule: Stable identifier of the check that reported the defect.
        field: Dotted path to the offending field, never its value.

    """

    rule: str
    field: str

    def as_context(self) -> dict[str, str]:
        """Return the defect as allowlisted error context."""
        return {"rule": self.rule, "field": self.field}


class PromptGrammarDefinitionError(AppError):
    """A grammar table violates its own construction invariants."""


class PromptGrammarIntegrityError(AppError):
    """The grammar tables do not match the digest pinned for this revision."""


class PromptCompositionError(AppError):
    """A prompt payload does not satisfy the section contract of its mode."""


class PromptAlignmentError(AppError):
    """A keyframe alignment mark cannot be expressed for the requested clip."""


class PromptValidationError(AppError):
    """A composed prompt fails one or more structural checks.

    Args:
        defects: Every defect found, so a caller can correct them in one pass
            rather than resubmitting once per rule.

    """

    def __init__(
        self,
        code: str,
        safe_message: str,
        *,
        defects: tuple[PromptDefect, ...] = (),
        retryable: bool = False,
        context: Mapping[str, str | int | float | bool | None] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        """Initialize a validation error carrying its full defect list."""
        super().__init__(
            code,
            safe_message,
            retryable=retryable,
            context=context,
            cause=cause,
        )
        self.defects = defects

    def to_safe_dict(self) -> dict[str, object]:
        """Return the serialization contract with every field-scoped defect."""
        payload = super().to_safe_dict()
        payload["defects"] = [defect.as_context() for defect in self.defects]
        return payload


class PromptRenderError(AppError):
    """A validated prompt cannot be rendered to wire text."""


class PromptContainmentError(AppError):
    """Caller-supplied text would act as grammar in the rendered prompt."""


class PromptProvenanceError(AppError):
    """A prompt digest or grammar revision cannot be bound to a result."""
