"""Pinned MiniMax H3 prompt grammar tables.

The vocabularies here are the input contract the H3 model was trained to accept.
They are authored from the upstream prompt-writing guides pinned in the plan at
``prompt-authoring.source_documents``; the guide text itself is deliberately not
vendored, because it ships under the MiniMax H3 Community License (see ADR-001).

Every table is frozen and covered by :data:`GRAMMAR_DIGEST`, which is verified at
import. Editing a table without restating the digest fails the import rather than
silently producing prompts under an unrecorded grammar.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Final

from tkr_cloud_video.core.errors import AppError

GRAMMAR_REVISION: Final[str] = "h3-2026-08"


class PromptGrammarDefinitionError(AppError):
    """A grammar table violates its own construction invariants."""


class PromptGrammarIntegrityError(AppError):
    """The grammar tables do not match the digest pinned for this revision."""


@dataclass(frozen=True, slots=True)
class Vocabulary:
    """A closed set of grammar values with an optionally omitted default.

    Args:
        name: Stable table name used in diagnostics and the digest.
        values: The permitted wire values, in canonical order.
        omitted_default: Label for the value that renders as nothing. It is
            deliberately absent from ``values`` because it has no wire form;
            medium camera amplitude and normal camera speed are the cases the
            upstream guide defines this way.

    """

    name: str
    values: tuple[str, ...]
    omitted_default: str | None = None

    def __post_init__(self) -> None:
        """Reject a table that cannot be rendered or resolved unambiguously."""
        if not self.values:
            raise PromptGrammarDefinitionError(
                "grammar_table_empty",
                "A grammar vocabulary declares no values.",
                context={"field": self.name},
            )
        if len(set(self.values)) != len(self.values):
            raise PromptGrammarDefinitionError(
                "grammar_table_duplicate_value",
                "A grammar vocabulary repeats a value.",
                context={"field": self.name},
            )
        if self.omitted_default is not None and self.omitted_default in self.values:
            raise PromptGrammarDefinitionError(
                "grammar_default_has_wire_form",
                "An omitted default must not also be a renderable value.",
                context={"field": self.name},
            )

    def permits(self, value: str) -> bool:
        """Report whether a value is renderable under this vocabulary."""
        return value in self.values


@dataclass(frozen=True, slots=True)
class CameraMotion:
    """One camera motion, with both the table label and its in-shot prose form.

    The guide requires camera motion to read as a natural English action inside
    the shot rather than a label appended to the sentence, so the prose form is
    part of the same fact and lives in the same table.
    """

    label: str
    phrase: str
    description: str


CAMERA_MOTIONS: Final[tuple[CameraMotion, ...]] = (
    CameraMotion("Zoom In", "zooms in", "focal length changes, camera body still"),
    CameraMotion("Zoom Out", "zooms out", "focal length changes, camera body still"),
    CameraMotion("Push In", "pushes in", "the camera moves forward"),
    CameraMotion("Pull Out", "pulls out", "the camera moves backward"),
    CameraMotion("Pan Left", "pans left", "the lens pivots horizontally in place"),
    CameraMotion("Pan Right", "pans right", "the lens pivots horizontally in place"),
    CameraMotion("Truck Left", "trucks left", "the camera translates horizontally"),
    CameraMotion("Truck Right", "trucks right", "the camera translates horizontally"),
    CameraMotion("Tilt Up", "tilts up", "the lens pivots vertically in place"),
    CameraMotion("Tilt Down", "tilts down", "the lens pivots vertically in place"),
    CameraMotion("Pedestal Up", "pedestals up", "the whole camera moves upward"),
    CameraMotion("Pedestal Down", "pedestals down", "the whole camera moves downward"),
    CameraMotion("Arc Shot", "arcs around the subject", "an arc around the subject"),
    CameraMotion("Tracking Shot", "tracks the subject", "follows a moving subject"),
    CameraMotion("Static Shot", "holds a static shot", "position and lens hold"),
    CameraMotion("Shake Slightly", "shakes slightly", "slight camera shake"),
    CameraMotion("Shake Strongly", "shakes strongly", "strong camera shake"),
    CameraMotion("POV", "shows the subject's point of view", "subject point of view"),
    CameraMotion("Roll Clockwise", "rolls clockwise", "rolls around the lens axis"),
    CameraMotion(
        "Roll Counterclockwise",
        "rolls counterclockwise",
        "rolls around the lens axis",
    ),
)

CAMERA_MOTION_TYPES: Final[Vocabulary] = Vocabulary(
    name="camera_motion_type",
    values=tuple(motion.label for motion in CAMERA_MOTIONS),
)
CAMERA_AMPLITUDES: Final[Vocabulary] = Vocabulary(
    name="camera_amplitude",
    values=("with small amplitude", "with large amplitude"),
    omitted_default="medium",
)
CAMERA_SPEEDS: Final[Vocabulary] = Vocabulary(
    name="camera_speed",
    values=("at slow speed", "at fast speed"),
    omitted_default="normal",
)
CUT_PHRASES: Final[Vocabulary] = Vocabulary(
    name="cut_phrase",
    values=(
        "the camera cuts to",
        "the shot cuts to",
        "the shot transitions to",
        "the shot changes to",
        "the shot switches to",
    ),
)
REQUESTED_TRANSITIONS: Final[Vocabulary] = Vocabulary(
    name="requested_transition",
    values=("cross-dissolve", "fade", "wipe"),
)
VISUAL_RETENTION_MARKERS: Final[Vocabulary] = Vocabulary(
    name="visual_retention_marker",
    values=(
        "fully_preserved",
        "partially_preserved",
        "attribute_transfer",
        "weak_reference",
    ),
)
AUDIO_RETENTION_MARKERS: Final[Vocabulary] = Vocabulary(
    name="audio_retention_marker",
    values=("fully_copy", "partially_copy", "reference", "weak_reference"),
)
TASK_TYPES: Final[Vocabulary] = Vocabulary(
    name="task_type",
    values=(
        "keyframe completion",
        "reference generation",
        "video editing",
        "video continuation",
        "audio reuse",
        "audio reference",
    ),
)
VISUAL_STYLES: Final[Vocabulary] = Vocabulary(
    name="visual_style",
    values=(
        "Cinematic",
        "live-action",
        "2D-animated",
        "3D CG",
        "claymation",
        "watercolor",
        "vintage film",
    ),
)

VOCABULARIES: Final[tuple[Vocabulary, ...]] = (
    CAMERA_MOTION_TYPES,
    CAMERA_AMPLITUDES,
    CAMERA_SPEEDS,
    CUT_PHRASES,
    REQUESTED_TRANSITIONS,
    VISUAL_RETENTION_MARKERS,
    AUDIO_RETENTION_MARKERS,
    TASK_TYPES,
    VISUAL_STYLES,
)

BASE_SECTIONS: Final[tuple[str, ...]] = (
    "integrated_multimodal_description",
    "overall_soundscape",
    "non_diegetic_music",
)
REFERENCE_SECTIONS: Final[tuple[str, ...]] = (
    "subject_definitions",
    "summary",
    "retention_analysis",
    "detailed_description",
    "overall_soundscape",
    "non_diegetic_music",
)
SECTION_ORDER: Final[dict[str, tuple[str, ...]]] = {
    "T2VA": BASE_SECTIONS,
    "I2VA": BASE_SECTIONS,
    "FL2VA": BASE_SECTIONS,
    "L2VA": BASE_SECTIONS,
    "Ref2VA": REFERENCE_SECTIONS,
}
KEYFRAME_MODES: Final[frozenset[str]] = frozenset({"I2VA", "FL2VA", "L2VA"})
MODES: Final[tuple[str, ...]] = tuple(SECTION_ORDER)

ABSENT_VALUE: Final[str] = "N/A"
UNINTELLIGIBLE_VALUE: Final[str] = "[unclear]"
VOICEOVER_PHRASE: Final[str] = "says in an off-screen voiceover"

DIALOGUE_OPEN: Final[str] = "<d>"
DIALOGUE_CLOSE: Final[str] = "</d>"
SCENE_TRANSITION_MARKER: Final[str] = "<scenetrans>"
CUTOFF_MARKER: Final[str] = "<cutoff>"
REFERENCE_LABEL_KINDS: Final[tuple[str, ...]] = ("Subject", "Picture", "Video", "Audio")

GRAMMAR_DIGEST: Final[str] = (
    "e216fdb8b3438e5b4d01bcb39b28bc90a5e64ca493918ee81901a74227f9bd84"
)


def grammar_fingerprint() -> str:
    """Return the SHA-256 digest of the pinned grammar tables.

    The digest covers every table that changes what a rendered prompt may say, so
    a committed result's recorded revision identifies an exact vocabulary set.
    """
    payload = {
        "revision": GRAMMAR_REVISION,
        "camera_motions": [[motion.label, motion.phrase] for motion in CAMERA_MOTIONS],
        "vocabularies": [
            [table.name, list(table.values), table.omitted_default]
            for table in VOCABULARIES
        ],
        "section_order": {mode: list(SECTION_ORDER[mode]) for mode in MODES},
        "tokens": [
            ABSENT_VALUE,
            UNINTELLIGIBLE_VALUE,
            VOICEOVER_PHRASE,
            DIALOGUE_OPEN,
            DIALOGUE_CLOSE,
            SCENE_TRANSITION_MARKER,
            CUTOFF_MARKER,
            list(REFERENCE_LABEL_KINDS),
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify_grammar_integrity(expected: str = GRAMMAR_DIGEST) -> None:
    """Fail closed when the tables no longer match the digest pinned for them.

    Args:
        expected: The digest the tables must reproduce. Defaults to the pinned
            revision digest and is injected only so the mismatch branch can be
            driven by a test without substituting the module.

    Raises:
        PromptGrammarIntegrityError: The tables were edited without restating
            :data:`GRAMMAR_DIGEST`, so the recorded revision would misdescribe
            the vocabulary a prompt was built from.

    """
    observed = grammar_fingerprint()
    if observed != expected:
        raise PromptGrammarIntegrityError(
            "grammar_digest_mismatch",
            "Prompt grammar tables do not match the pinned revision digest.",
            context={"field": "grammar", "rule": GRAMMAR_REVISION},
        )
