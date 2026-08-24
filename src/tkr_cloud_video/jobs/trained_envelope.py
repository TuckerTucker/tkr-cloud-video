"""Pinned per-model-set trained envelopes.

A generation request that names no dimensions has to land somewhere. Bare module
constants put it wherever the first model happened to be trained, and would keep
putting it there after a second model set arrived — silently misdescribing it.
The envelopes here are keyed by model set instead, so resolving defaults for an
unregistered model set refuses rather than guesses.

Every table is frozen and covered by :data:`ENVELOPE_DIGEST`, which is verified
at import. Editing an envelope without restating the digest fails the import
rather than silently changing where default requests land. This mirrors
:mod:`tkr_cloud_video.prompting.grammar`, which pins the prompt vocabulary the
same way and for the same reason.

The envelope is deliberately not a field on
:class:`~tkr_cloud_video.artifacts.models.ModelSetManifest`: the manifest's
canonical bytes are its identity, and a reviewer-signed license approval binds
that exact digest. Adding a field there would invalidate every existing approval
and require a human re-signature to describe a fact the manifest never carried.

What a model-set identity names
-------------------------------
The published identifier ``minimax-h3-t2v-int8-20260809`` says text-to-video
while the diffusion checkpoint it ships is ``minimax-h3-fl2va-int8``, the
keyframe first/last-frame-to-video-and-audio checkpoint, and its graph reaches
text-to-video by driving ``MiniMaxH3ImageToVideo`` with no image attached. The
segment therefore already names *the mode the published graph drives*, not the
weights. That was never written down, and this registry is the first place where
guessing wrong would be load-bearing, so it is decided and recorded here.

A model-set identity names the published set — weights, workflow graph and
binding map together — and not the checkpoint. Two consequences follow, and both
are why the reference set is a separate row rather than a reuse of the existing
one:

* Naming the checkpoint would collapse the text-to-video set and the
  reference-conditioned set onto one identifier, since they ship byte-identical
  weights. One identifier is one envelope, so the reference set would inherit
  the text-to-video row by construction — exactly the silent inheritance this
  registry exists to prevent, and unreviewable because no second row would ever
  exist to review.
* Renaming what is already published is not available either. A reviewer license
  approval binds the manifest digest, and ``model_set_id`` is inside the
  manifest's canonical bytes, so correcting the published identifier would
  invalidate a signed approval to fix a name that is, under the rule above, not
  wrong.

So ``minimax-h3-ref2v-int8-20260809`` is its own model set with its own row. It
shares the ``20260809`` segment with the text-to-video set on purpose and not by
oversight: that segment names the weights snapshot, and both sets ship the same
four artifacts from the same source revision. The ``ref2v`` segment is what
separates them, which is the segment the rule above says carries the meaning.

Its numbers are the same as the text-to-video set's, because the trained canvas
and temporal grid are properties of the fl2va checkpoint both sets ship. They are
*restated* under this identity rather than inherited through it: keying on the
full identifier is what forced someone to affirm that, and a future set whose
weights differ cannot pick these numbers up by prefix.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from tkr_cloud_video.core.errors import AppError

ENVELOPE_REVISION: Final[str] = "h3-2026-08"


class TrainedEnvelopeDefinitionError(AppError):
    """An envelope table violates its own construction invariants."""


class TrainedEnvelopeIntegrityError(AppError):
    """The envelope tables do not match the digest pinned for this revision."""


class UnregisteredModelSetError(AppError):
    """No trained envelope is registered for the requested model set."""


@dataclass(frozen=True, slots=True)
class TrainedRange:
    """The canvas and frame range one model set was trained on.

    Args:
        model_set_id: The model set this range describes, exactly as requests
            name it. Keying on the full identifier is deliberate: a rebuild
            mints a new identifier, and someone has to affirm the envelope still
            holds rather than inherit it by prefix.
        short_edge: The trained short edge, and the default height.
        long_edge: The trained long edge, and the default width. Also the
            hard ceiling for either axis.
        min_frames: The trained frame floor, and the default frame count.
        max_frames: The hard frame ceiling. Above the trained range there is no
            cheap use to protect, so this bounds rather than reports.
        grid_stride: Temporal grid stride the frame count must satisfy.
        grid_offset: Temporal grid offset the frame count must satisfy.
        canvas_multiple: Both canvas axes must be a multiple of this.
        source: What the numbers were verified against, so a reader can check
            them by hand. The upstream declaration is not machine-readable from
            here, so the basis is stated instead of compared.

    """

    model_set_id: str
    short_edge: int
    long_edge: int
    min_frames: int
    max_frames: int
    grid_stride: int
    grid_offset: int
    canvas_multiple: int
    source: str

    def __post_init__(self) -> None:
        """Reject an envelope that cannot describe a reachable request."""
        if self.short_edge > self.long_edge:
            raise TrainedEnvelopeDefinitionError(
                "envelope_edges_inverted",
                "A trained envelope declares a short edge above its long edge.",
                context={"resource_id": self.model_set_id},
            )
        if self.min_frames > self.max_frames:
            raise TrainedEnvelopeDefinitionError(
                "envelope_frames_inverted",
                "A trained envelope declares a frame floor above its ceiling.",
                context={"resource_id": self.model_set_id},
            )
        if self.canvas_multiple <= 0 or self.grid_stride <= 0:
            raise TrainedEnvelopeDefinitionError(
                "envelope_step_not_positive",
                "A trained envelope declares a non-positive step.",
                context={"resource_id": self.model_set_id},
            )
        for edge in (self.short_edge, self.long_edge):
            if edge % self.canvas_multiple != 0:
                raise TrainedEnvelopeDefinitionError(
                    "envelope_edge_off_grid",
                    "A trained envelope declares an edge off its own canvas grid.",
                    context={"resource_id": self.model_set_id},
                )
        if not self.on_frame_grid(self.min_frames):
            raise TrainedEnvelopeDefinitionError(
                "envelope_frames_off_grid",
                "A trained envelope declares a frame floor off its own grid.",
                context={"resource_id": self.model_set_id},
            )

    @property
    def default_width(self) -> int:
        """Return the width a request that names none should receive."""
        return self.long_edge

    @property
    def default_height(self) -> int:
        """Return the height a request that names none should receive."""
        return self.short_edge

    @property
    def default_frames(self) -> int:
        """Return the frame count a request that names none should receive."""
        return self.min_frames

    def on_frame_grid(self, frames: int) -> bool:
        """Report whether a frame count satisfies this model's temporal grid."""
        return (frames - self.grid_offset) % self.grid_stride == 0

    def within_canvas(self, width: int, height: int) -> bool:
        """Report whether a canvas is inside the model's supported bounds.

        A short edge at or below the trained short edge and a long edge at or
        below the trained long edge together imply the node's pixel-area cap, so
        the area needs no separate check.
        """
        return (
            min(width, height) <= self.short_edge
            and max(width, height) <= self.long_edge
        )

    def departure(self, width: int, height: int, frames: int) -> EnvelopeDeparture:
        """Report where a request sits relative to this trained range."""
        return EnvelopeDeparture(
            model_set_id=self.model_set_id,
            short_edge_below_trained=min(width, height) < self.short_edge,
            frames_below_trained=frames < self.min_frames,
        )


@dataclass(frozen=True, slots=True)
class EnvelopeDeparture:
    """Where a request sits relative to the range its model was trained on.

    Below the trained range a request is cheaper and is a legitimate smoke test,
    so it stays permitted and is reported instead of refused. Above it there is
    no cheap use to protect, so the ceiling bounds rather than reports.

    Args:
        model_set_id: The model set whose envelope this was measured against,
            so a recorded departure cannot be read against the wrong model.
        short_edge_below_trained: The short edge is under the trained short
            edge, so the canvas is smaller than any trained sample.
        frames_below_trained: Fewer frames than the trained floor.

    """

    model_set_id: str
    short_edge_below_trained: bool
    frames_below_trained: bool

    @property
    def inside(self) -> bool:
        """Report whether the request sits wholly inside the trained range."""
        return not (self.short_edge_below_trained or self.frames_below_trained)

    def as_metadata(self) -> dict[str, object]:
        """Return the departure as fields for the per-attempt generation record."""
        return {
            "trained_envelope_model_set_id": self.model_set_id,
            "trained_envelope_inside": self.inside,
            "trained_envelope_short_edge_below": self.short_edge_below_trained,
            "trained_envelope_frames_below": self.frames_below_trained,
        }


# Verified against Comfy-Org/ComfyUI @ dec5d9450a5290bcf63430409ea41018e67f41c3,
# comfy_extras/nodes_minimax_h3.py — the revision this worker image pins. The node
# declares width=1344, height=768, length=124 as its own defaults, and its length
# tooltip states: "Frame count at 24 fps, snapped up to the model's 17k+5 grid
# (124 = ~5s; trained range is ~124-362, longer is untested)".
MINIMAX_H3_SOURCE: Final[str] = (
    "Comfy-Org/ComfyUI@dec5d9450a5290bcf63430409ea41018e67f41c3"
    " comfy_extras/nodes_minimax_h3.py"
)

TEXT_TO_VIDEO_MODEL_SET_ID: Final[str] = "minimax-h3-t2v-int8-20260809"

# The reference-conditioned set's identity, decided in the module docstring and
# named here so the release catalog, the license approval and the console all
# spell the one identifier rather than three that happen to agree. It is a new
# identity rather than a reuse because a model-set identity names the published
# set — weights, graph and bindings — and the reference graph differs.
REFERENCE_MODEL_SET_ID: Final[str] = "minimax-h3-ref2v-int8-20260809"

# The same node module at the same pinned revision, because the reference set
# ships the identical fl2va checkpoint under a different graph; the trained
# canvas and temporal grid are the checkpoint's, so they are restated here
# rather than reached for through the text-to-video row.
REFERENCE_ENVELOPE_SOURCE: Final[str] = (
    f"{MINIMAX_H3_SOURCE};"
    " weights identical to minimax-h3-t2v-int8-20260809"
    " (Comfy-Org/MiniMax-H3@014cd40f7e177756c6b2473c0d93b1c89a790dd2)"
)

TRAINED_RANGES: Final[dict[str, TrainedRange]] = {
    TEXT_TO_VIDEO_MODEL_SET_ID: TrainedRange(
        model_set_id=TEXT_TO_VIDEO_MODEL_SET_ID,
        short_edge=768,
        long_edge=1344,
        min_frames=124,
        max_frames=362,
        grid_stride=17,
        grid_offset=5,
        canvas_multiple=32,
        source=MINIMAX_H3_SOURCE,
    ),
    REFERENCE_MODEL_SET_ID: TrainedRange(
        model_set_id=REFERENCE_MODEL_SET_ID,
        short_edge=768,
        long_edge=1344,
        min_frames=124,
        max_frames=362,
        grid_stride=17,
        grid_offset=5,
        canvas_multiple=32,
        source=REFERENCE_ENVELOPE_SOURCE,
    ),
}

# Restated for the reference row. :data:`ENVELOPE_REVISION` is unchanged and
# deliberately so: it labels the upstream declaration every row is read from,
# and adding a row moved no existing default. The digest, not the revision
# label, is what identifies the exact table a committed result ran under.
ENVELOPE_DIGEST: Final[str] = (
    "22d6bf1e96a8cd4143a20fd5e9724e220b45d71f873df8f0757fea00e02d4c62"
)


def envelope_fingerprint() -> str:
    """Return the SHA-256 digest of the pinned envelope tables.

    The digest covers every number that changes where a default request lands or
    which requests are refused, so a committed result's recorded revision
    identifies an exact envelope set.
    """
    payload = {
        "revision": ENVELOPE_REVISION,
        "ranges": [
            [
                entry.model_set_id,
                entry.short_edge,
                entry.long_edge,
                entry.min_frames,
                entry.max_frames,
                entry.grid_stride,
                entry.grid_offset,
                entry.canvas_multiple,
                entry.source,
            ]
            for entry in sorted(TRAINED_RANGES.values(), key=lambda e: e.model_set_id)
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify_envelope_integrity(expected: str = ENVELOPE_DIGEST) -> None:
    """Fail closed when the tables no longer match the digest pinned for them.

    Args:
        expected: The digest the tables must reproduce. Defaults to the pinned
            revision digest and is injected only so the mismatch branch can be
            driven by a test without substituting the module.

    Raises:
        TrainedEnvelopeIntegrityError: The tables were edited without restating
            :data:`ENVELOPE_DIGEST`, so default requests would land somewhere
            the recorded revision does not describe.

    """
    observed = envelope_fingerprint()
    if observed != expected:
        raise TrainedEnvelopeIntegrityError(
            "envelope_digest_mismatch",
            "Trained envelope tables do not match the pinned revision digest.",
            context={"field": "trained_envelope", "rule": ENVELOPE_REVISION},
        )


def resolve_trained_range(model_set_id: str) -> TrainedRange:
    """Return the trained envelope registered for a model set.

    Args:
        model_set_id: The model set a request names.

    Returns:
        The envelope pinned for that model set.

    Raises:
        UnregisteredModelSetError: No envelope is registered. Refusing is the
            point: inheriting another model's canvas and frame range is exactly
            the silent misdescription this registry exists to prevent.

    """
    entry = TRAINED_RANGES.get(model_set_id)
    if entry is None:
        raise UnregisteredModelSetError(
            "model_set_envelope_unregistered",
            "No trained envelope is registered for this model set.",
            context={"resource_id": model_set_id, "rule": ENVELOPE_REVISION},
        )
    return entry


def unregistered_model_sets(observed: Iterable[str]) -> tuple[str, ...]:
    """Return observed model sets this registry does not cover.

    Reading the registry forward answers only whether each listed envelope is
    well-formed, which cannot see a model set the registry forgot. The observed
    population is injected rather than discovered here so a test can drive this
    with a population of one, and so the caller declares where it looked.

    Args:
        observed: Model set identifiers found in the real population — the
            release catalogs on disk.

    Returns:
        The observed identifiers with no registered envelope, in sorted order.

    """
    return tuple(sorted(set(observed) - set(TRAINED_RANGES)))


def envelope_report(observed: Iterable[str] = ()) -> dict[str, object]:
    """Return the doctor's read-only view of the pinned envelopes.

    Args:
        observed: Model set identifiers found on disk, so the report can name
            any the registry omits rather than certifying a subject nobody
            examined.

    """
    unregistered = unregistered_model_sets(observed)
    return {
        "revision": ENVELOPE_REVISION,
        "observed_digest": envelope_fingerprint(),
        "digest_verified": envelope_fingerprint() == ENVELOPE_DIGEST,
        "registered_model_sets": tuple(sorted(TRAINED_RANGES)),
        "unregistered_model_sets": unregistered,
    }


verify_envelope_integrity()
