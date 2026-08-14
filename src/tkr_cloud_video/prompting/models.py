"""Typed structured MiniMax H3 prompts.

Caller-supplied text is typed distinctly from service-authored description:
:attr:`DialogueLine.text` and :attr:`Shot.on_screen_text` are the only fields a
caller controls, and they are the only fields the containment rules police. Every
other string is authored by this service and may legitimately carry grammar.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from tkr_cloud_video.prompting.errors import PromptCompositionError
from tkr_cloud_video.prompting.grammar import ABSENT_VALUE, SECTION_ORDER

Mode = Literal["T2VA", "I2VA", "FL2VA", "L2VA", "Ref2VA"]

REFERENCE_ONLY_SECTIONS: Final[frozenset[str]] = frozenset(
    {"subject_definitions", "summary", "retention_analysis"}
)
SPEAKER_PATTERN: Final[str] = r"^S[1-9][0-9]?(,S[1-9][0-9]?)*$"
LABEL_PATTERN: Final[str] = r"^<(Subject|Picture|Video|Audio) [1-9][0-9]?>$"


class _Frozen(BaseModel):
    """Immutable base that refuses unknown fields at the boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=False)


class CameraDirection(_Frozen):
    """Motion type with the two modifiers the grammar allows to be omitted."""

    motion: str = Field(min_length=1)
    amplitude: str | None = None
    speed: str | None = None


class DialogueLine(_Frozen):
    """One spoken, sung, or voiced-over line.

    Args:
        speaker_id: Stable id, or a compound id when numbered speakers vocalize
            together.
        identity: Service-authored voice and delivery description, rendered
            outside the dialogue delimiters.
        language: Language tag rendered inside the delimiters.
        text: Caller-supplied. Rendered verbatim and never translated.

    """

    speaker_id: Annotated[str, Field(pattern=SPEAKER_PATTERN)]
    identity: str = Field(default="", max_length=300)
    language: str = Field(min_length=1, max_length=40)
    text: str = Field(min_length=1, max_length=1000)
    voiceover: bool = False
    continues_across_cut: bool = False
    truncated_by_end: bool = False


class Shot(_Frozen):
    """One shot on the target video's timeline."""

    number: int = Field(ge=1)
    cut_at_seconds: float | None = Field(default=None, ge=0)
    cut_phrase: str | None = None
    style: str | None = None
    description: str = Field(min_length=1, max_length=2000)
    camera: CameraDirection | None = None
    dialogue: tuple[DialogueLine, ...] = ()
    on_screen_text: tuple[str, ...] = ()
    labels: tuple[Annotated[str, Field(pattern=LABEL_PATTERN)], ...] = ()


class ReferenceDefinition(_Frozen):
    """One entry of the full-reference definitions section."""

    label: Annotated[str, Field(pattern=LABEL_PATTERN)]
    definition: str = Field(min_length=1, max_length=600)
    speaker_id: Annotated[str, Field(pattern=r"^S[1-9][0-9]?$")] | None = None


class PromptSummary(_Frozen):
    """The bracketed task-type prefix and the summary paragraph."""

    task_types: tuple[str, ...] = Field(min_length=1)
    text: str = Field(min_length=1, max_length=1200)


class RetentionEntry(_Frozen):
    """How one referenced item is preserved, transferred, copied, or referenced."""

    label: Annotated[str, Field(pattern=LABEL_PATTERN)]
    marker: str = Field(min_length=1)
    scope: str = Field(default="", max_length=200)
    detail: str = Field(min_length=1, max_length=600)


class StructuredPrompt(_Frozen):
    """A complete H3 prompt for one generation mode."""

    mode: Mode
    duration_seconds: float = Field(gt=0, le=60)
    shots: tuple[Shot, ...] = Field(min_length=1)
    overall_soundscape: str = Field(min_length=1, max_length=1200)
    non_diegetic_music: str = Field(min_length=1, max_length=1200)
    subject_definitions: tuple[ReferenceDefinition, ...] = ()
    summary: PromptSummary | None = None
    retention_analysis: tuple[RetentionEntry, ...] = ()
    grammar_revision: str | None = None

    @property
    def section_order(self) -> tuple[str, ...]:
        """Return the section order this prompt's mode fixes."""
        return SECTION_ORDER[self.mode]

    @property
    def is_reference_mode(self) -> bool:
        """Report whether this prompt uses the full-reference section set."""
        return self.mode == "Ref2VA"


def compose_prompt(payload: Mapping[str, Any]) -> StructuredPrompt:
    """Compose a structured prompt from an external payload.

    Section coherence is checked against the mode before field validation runs, so
    a caller learns every section defect at once rather than one per round trip.

    Args:
        payload: The external prompt document, key order significant.

    Returns:
        The composed immutable prompt.

    Raises:
        PromptCompositionError: The payload omits a section its mode requires,
            carries one its mode forbids, or orders its sections differently from
            the order the mode fixes.

    """
    mode = payload.get("mode")
    if not isinstance(mode, str) or mode not in SECTION_ORDER:
        raise PromptCompositionError(
            "prompt_mode_unknown",
            "Prompt declares no supported generation mode.",
            context={"field": "mode"},
        )

    defects = _section_defects(mode, payload)
    if defects:
        raise PromptCompositionError(
            "section_set_mismatch_for_mode",
            "Prompt sections do not match the set this mode fixes.",
            context={"field": ",".join(sorted(defects)), "rule": mode},
        )

    return StructuredPrompt.model_validate(dict(payload))


def _section_defects(mode: str, payload: Mapping[str, Any]) -> set[str]:
    """Return the section names that are missing, forbidden, or out of order."""
    defects: set[str] = set()
    required = _required_sections(mode)

    defects.update(name for name in required if payload.get(name) in (None, (), []))
    if mode != "Ref2VA":
        defects.update(name for name in REFERENCE_ONLY_SECTIONS if name in payload)

    observed = [key for key in payload if key in required]
    expected = [name for name in required if name in observed]
    if observed != expected:
        defects.update(observed)
    return defects


def _required_sections(mode: str) -> tuple[str, ...]:
    """Return the payload keys a mode requires, in the order it fixes them.

    The shot list is supplied as ``shots`` in every mode; which rendered section
    it becomes - ``integrated_multimodal_description`` or ``detailed_description``
    - is a rendering concern held by :data:`SECTION_ORDER`.
    """
    if mode == "Ref2VA":
        return (
            "subject_definitions",
            "summary",
            "retention_analysis",
            "shots",
            "overall_soundscape",
            "non_diegetic_music",
        )
    return ("shots", "overall_soundscape", "non_diegetic_music")


def absent_or(value: str | None) -> str:
    """Return the grammar's absent-value token when a section carries no content."""
    return value if value else ABSENT_VALUE


def speaker_ids(shots: Sequence[Shot]) -> tuple[str, ...]:
    """Return every distinct speaker id in first-vocalization order."""
    seen: list[str] = []
    for shot in shots:
        for line in shot.dialogue:
            for identifier in line.speaker_id.split(","):
                if identifier not in seen:
                    seen.append(identifier)
    return tuple(seen)
