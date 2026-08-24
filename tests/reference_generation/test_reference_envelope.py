"""Reference model-set envelope registration, identity, and integrity tests.

Slice 2 coverage. These tests assert that the reference model set has an
envelope row of its own, that the row is covered by the restated digest, and
that a request naming an unregistered set is refused rather than handed the
text-to-video set's numbers. They deliberately do not claim a reference request
can execute: the graph, the manifest and the binding arrive in slices 1, 3 and 4.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.jobs.contracts import parse_generation_request
from tkr_cloud_video.jobs.trained_envelope import (
    ENVELOPE_DIGEST,
    REFERENCE_MODEL_SET_ID,
    TEXT_TO_VIDEO_MODEL_SET_ID,
    TRAINED_RANGES,
    TrainedRange,
    UnregisteredModelSetError,
    envelope_fingerprint,
    resolve_trained_range,
    verify_envelope_integrity,
)

ENVELOPE_MODULE = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "tkr_cloud_video"
    / "jobs"
    / "trained_envelope.py"
)

# The unregistered set is spelled as a well-formed identifier that shares the
# reference set's prefix. Sharing the prefix is the point: if resolution ever
# widened from the full identifier to a family, this is the payload that would
# start silently succeeding.
UNREGISTERED_MODEL_SET = "minimax-h3-ref2v-int8-20991231"


def reference_payload(**overrides: object) -> dict[str, object]:
    """Build a reference-to-video request naming nothing about dimensions."""
    body: dict[str, object] = {
        "mode": "reference-to-video",
        "workflow_id": "wf-ref-1",
        "model_set_id": REFERENCE_MODEL_SET_ID,
        "prompt": "A safe synthetic prompt",
        "seed": 11,
        "references": [{"object_key": "inputs/principal/one.png"}],
    }
    body.update(overrides)
    return body


def test_the_reference_model_set_has_a_row_of_its_own() -> None:
    """S2-T02. The reference set is registered under its own identity.

    Not a rename of the published text-to-video set and not an alias of it. The
    two identities are distinct keys holding distinct entries, which is what
    makes the reference envelope a thing somebody affirmed rather than a thing
    inherited by sharing a checkpoint.
    """
    reference = TRAINED_RANGES[REFERENCE_MODEL_SET_ID]
    text_to_video = TRAINED_RANGES[TEXT_TO_VIDEO_MODEL_SET_ID]

    assert REFERENCE_MODEL_SET_ID != TEXT_TO_VIDEO_MODEL_SET_ID
    assert reference is not text_to_video
    assert reference.model_set_id == REFERENCE_MODEL_SET_ID
    assert reference.source != text_to_video.source
    assert reference.source
    assert resolve_trained_range(REFERENCE_MODEL_SET_ID) is reference


def test_a_reference_request_naming_no_dimensions_gets_the_registered_ones() -> None:
    """S2-T02. Absent dimensions are filled from the reference set's own row."""
    request = parse_generation_request(reference_payload())

    entry = TRAINED_RANGES[REFERENCE_MODEL_SET_ID]

    assert (request.width, request.height, request.frames) == (
        entry.long_edge,
        entry.short_edge,
        entry.min_frames,
    )
    assert request.trained_envelope().inside


def test_the_reference_defaults_come_from_registration_not_from_the_prefix() -> None:
    """S2-T02. Withdraw the row and the same request stops resolving.

    Registration is doing the work, not the shared ``minimax-h3`` family name
    and not the byte-identical checkpoint. Parsed against a population holding
    only the text-to-video row, this request is refused rather than quietly
    given that row's numbers.
    """
    text_to_video_only = {
        TEXT_TO_VIDEO_MODEL_SET_ID: TRAINED_RANGES[TEXT_TO_VIDEO_MODEL_SET_ID]
    }

    with pytest.raises(ValidationError) as raised:
        parse_generation_request(reference_payload(), text_to_video_only)

    assert "no trained envelope is registered" in str(raised.value)


def test_an_unregistered_reference_model_set_is_refused_naming_it() -> None:
    """S2-T01. The refusal carries the identifier the operator has to fix."""
    with pytest.raises(UnregisteredModelSetError) as raised:
        resolve_trained_range(UNREGISTERED_MODEL_SET)

    assert raised.value.code == "model_set_envelope_unregistered"
    assert raised.value.context["resource_id"] == UNREGISTERED_MODEL_SET


def test_intake_refuses_an_unregistered_model_set_rather_than_defaulting() -> None:
    """S2-T01. Intake refuses at the boundary, before any dimension is filled."""
    with pytest.raises(ValidationError) as raised:
        parse_generation_request(reference_payload(model_set_id=UNREGISTERED_MODEL_SET))

    assert "no trained envelope is registered" in str(raised.value)


def test_supplying_dimensions_does_not_buy_an_unregistered_set_an_envelope() -> None:
    """S2-T01. Naming valid-looking numbers is not a substitute for a row."""
    entry = TRAINED_RANGES[REFERENCE_MODEL_SET_ID]

    with pytest.raises(ValidationError):
        parse_generation_request(
            reference_payload(
                model_set_id=UNREGISTERED_MODEL_SET,
                width=entry.long_edge,
                height=entry.short_edge,
                frames=entry.min_frames,
            )
        )


def test_a_reference_request_below_the_trained_floor_is_reported_not_refused() -> None:
    """The floor reports and the ceiling bounds, for this set as for the other.

    Below the trained range a request is cheaper and is a legitimate smoke test,
    so it stays permitted and merely stops being silent.
    """
    entry = TRAINED_RANGES[REFERENCE_MODEL_SET_ID]
    small_frames = entry.min_frames - entry.grid_stride

    request = parse_generation_request(
        reference_payload(width=512, height=512, frames=small_frames)
    )

    departure = request.trained_envelope()

    assert not departure.inside
    assert departure.short_edge_below_trained
    assert departure.frames_below_trained
    assert (
        departure.as_metadata()["trained_envelope_model_set_id"]
        == REFERENCE_MODEL_SET_ID
    )


def test_a_reference_request_above_the_trained_ceiling_is_refused() -> None:
    """Above the trained range there is no cheap use to protect."""
    entry = TRAINED_RANGES[REFERENCE_MODEL_SET_ID]

    for change in (
        {"width": entry.long_edge + entry.canvas_multiple},
        {"frames": entry.max_frames + entry.grid_stride},
    ):
        with pytest.raises(ValidationError):
            parse_generation_request(reference_payload(**change))


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        ({"width": 1330, "height": 768}, "multiple of 32"),
        ({"width": 1344, "height": 750}, "multiple of 32"),
        ({"frames": 125}, "17k+5 grid"),
    ],
)
def test_an_off_grid_reference_request_is_refused_naming_its_own_rule(
    change: dict[str, Any], rule: str
) -> None:
    """S2-TF. Each defect is reported against the rule it broke.

    A canvas off the multiple and a frame count off the temporal grid are two
    different mistakes, and a caller told only that some value was invalid
    cannot tell which one it made. Both edges are checked, so neither axis is
    inferred from the other.
    """
    with pytest.raises(ValidationError) as raised:
        parse_generation_request(reference_payload(**change))

    message = str(raised.value)

    assert rule in message
    assert str(change) not in message


def test_the_restated_digest_covers_the_shipped_tables() -> None:
    """S2-T03. The digest shipped beside the tables is the tables' digest."""
    assert envelope_fingerprint() == ENVELOPE_DIGEST
    verify_envelope_integrity()


def test_editing_the_reference_row_breaks_the_pinned_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S2-T03. The reference row is inside the digest, not merely beside it.

    The row is edited in place against the pinned digest, so this fails if the
    new entry were ever excluded from the fingerprint payload — which reading
    the digest forward could not detect.
    """
    entry = TRAINED_RANGES[REFERENCE_MODEL_SET_ID]
    edited = TrainedRange(
        model_set_id=entry.model_set_id,
        short_edge=entry.short_edge,
        long_edge=entry.long_edge,
        min_frames=entry.min_frames + entry.grid_stride,
        max_frames=entry.max_frames,
        grid_stride=entry.grid_stride,
        grid_offset=entry.grid_offset,
        canvas_multiple=entry.canvas_multiple,
        source=entry.source,
    )
    monkeypatch.setitem(TRAINED_RANGES, REFERENCE_MODEL_SET_ID, edited)

    assert envelope_fingerprint() != ENVELOPE_DIGEST


def test_an_edited_default_fails_at_import_not_at_request_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S2-T03. The shipped module refuses to load with a stale digest.

    The real module source is edited and imported, so this drives the
    import-time guard itself rather than a hand-called verification. Importing
    is what a changed default would have to survive to reach a request, and it
    does not: the failure lands on whoever edited the table, not on a caller who
    would have been served a canvas the recorded revision does not describe.
    """
    source = ENVELOPE_MODULE.read_text(encoding="utf-8")
    marker = "        model_set_id=REFERENCE_MODEL_SET_ID,\n        short_edge=768,"

    assert source.count(marker) == 1

    edited = source.replace(
        marker, "        model_set_id=REFERENCE_MODEL_SET_ID,\n        short_edge=512,"
    )
    copy = tmp_path / "edited_trained_envelope.py"
    copy.write_text(edited, encoding="utf-8")

    module_name = "edited_trained_envelope"
    spec = importlib.util.spec_from_file_location(module_name, copy)

    assert spec is not None and spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)

    with pytest.raises(AppError) as raised:
        spec.loader.exec_module(module)

    assert raised.value.code == "envelope_digest_mismatch"
    assert "512" not in str(raised.value.context)
