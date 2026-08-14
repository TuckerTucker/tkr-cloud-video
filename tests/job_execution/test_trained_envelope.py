"""Per-model-set trained envelope resolution, integrity, and omission tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from tkr_cloud_video.bootstrap import observed_model_sets, run_doctor
from tkr_cloud_video.jobs.contracts import parse_generation_request
from tkr_cloud_video.jobs.trained_envelope import (
    ENVELOPE_DIGEST,
    TRAINED_RANGES,
    EnvelopeDeparture,
    TrainedEnvelopeDefinitionError,
    TrainedEnvelopeIntegrityError,
    TrainedRange,
    UnregisteredModelSetError,
    envelope_fingerprint,
    envelope_report,
    resolve_trained_range,
    unregistered_model_sets,
    verify_envelope_integrity,
)

OTHER_MODEL_SET = "other-model-int8-20270101"

# A deliberately different envelope: a square trained canvas on a 16-pixel grid
# with an 8k+1 temporal grid. Every number differs from MiniMax H3's, so a
# default resolved from H3's constants cannot accidentally satisfy it.
OTHER_RANGE = TrainedRange(
    model_set_id=OTHER_MODEL_SET,
    short_edge=512,
    long_edge=512,
    min_frames=33,
    max_frames=97,
    grid_stride=8,
    grid_offset=1,
    canvas_multiple=16,
    source="synthetic fixture for envelope-resolution tests",
)
OTHER_ONLY = {OTHER_MODEL_SET: OTHER_RANGE}


def payload(model_set_id: str, **overrides: object) -> dict[str, object]:
    """Build a request naming a given model set and nothing about dimensions."""
    body: dict[str, object] = {
        "mode": "text-to-video",
        "workflow_id": "wf-1",
        "model_set_id": model_set_id,
        "prompt": "A safe synthetic prompt",
        "seed": 7,
    }
    body.update(overrides)
    return body


def test_defaults_come_from_the_named_model_set_not_from_constants() -> None:
    """A different model set resolves to its own envelope, not MiniMax H3's.

    This is the assertion the previous design could not make. With the envelope
    held in module constants, every request landed on 1344x768x124 whatever it
    named, so a second model set was silently misdescribed rather than served.
    """
    request = parse_generation_request(payload(OTHER_MODEL_SET), OTHER_ONLY)

    assert (request.width, request.height) == (512, 512)
    assert request.frames == 33
    assert request.trained_envelope(OTHER_ONLY).inside


def test_the_pinned_model_set_still_resolves_to_its_own_declaration() -> None:
    """The registered envelope is unchanged by becoming per-model-set."""
    h3 = TRAINED_RANGES["minimax-h3-t2v-int8-20260809"]
    request = parse_generation_request(payload(h3.model_set_id))

    assert (request.width, request.height, request.frames) == (1344, 768, 124)
    assert (h3.long_edge, h3.short_edge, h3.min_frames) == (1344, 768, 124)


def test_bounds_follow_the_named_model_set_too() -> None:
    """Refusals are measured against the named model's envelope, not H3's.

    1344x768 is inside MiniMax H3's canvas and outside this one; 124 frames sits
    on H3's grid and off this one. Both are refused here, which is what proves
    the bound moved with the model rather than staying pinned to a constant.
    """
    for change in (
        {"width": 1344, "height": 768},
        {"frames": 124},
        {"frames": OTHER_RANGE.max_frames + OTHER_RANGE.grid_stride},
    ):
        with pytest.raises(ValidationError):
            parse_generation_request(payload(OTHER_MODEL_SET, **change), OTHER_ONLY)


def test_a_sub_envelope_request_is_reported_against_its_own_model() -> None:
    """The departure names the model it was measured against."""
    request = parse_generation_request(
        payload(OTHER_MODEL_SET, width=256, height=256, frames=17), OTHER_ONLY
    )

    departure = request.trained_envelope(OTHER_ONLY)

    assert not departure.inside
    assert departure.short_edge_below_trained
    assert departure.frames_below_trained
    assert departure.as_metadata()["trained_envelope_model_set_id"] == OTHER_MODEL_SET


def test_an_unregistered_model_set_is_refused_not_defaulted() -> None:
    """Inheriting another model's envelope is the failure this prevents."""
    with pytest.raises(ValidationError) as raised:
        parse_generation_request(payload("nobody-published-this-20990101"))

    assert "no trained envelope is registered" in str(raised.value)


def test_an_unregistered_model_set_is_refused_even_with_dimensions_supplied() -> None:
    """Supplying dimensions does not buy a request an unreviewed envelope."""
    with pytest.raises(ValidationError):
        parse_generation_request(
            payload("nobody-published-this-20990101", width=512, height=512, frames=33)
        )


def test_a_malformed_model_set_reports_its_own_defect() -> None:
    """Identifier syntax is reported as such, not as a missing envelope."""
    with pytest.raises(ValidationError) as raised:
        parse_generation_request(payload("../unsafe"))

    assert "no trained envelope is registered" not in str(raised.value)


def test_resolve_names_the_model_set_it_could_not_find() -> None:
    """The refusal carries the identifier, so an operator sees which one."""
    with pytest.raises(UnregisteredModelSetError) as raised:
        resolve_trained_range("absent-model-20990101")

    assert raised.value.context["resource_id"] == "absent-model-20990101"


def test_pinned_tables_match_their_digest() -> None:
    """The shipped tables reproduce the digest pinned for them."""
    assert envelope_fingerprint() == ENVELOPE_DIGEST
    verify_envelope_integrity()


def test_editing_a_table_without_restating_the_digest_fails_closed() -> None:
    """A digest that does not describe the tables is refused at verification.

    The expected digest is injected rather than the module substituted, so this
    drives the real mismatch branch without a mock.
    """
    with pytest.raises(TrainedEnvelopeIntegrityError) as raised:
        verify_envelope_integrity("0" * 64)

    assert raised.value.code == "envelope_digest_mismatch"


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"short_edge": 2048}, "envelope_edges_inverted"),
        ({"min_frames": 200}, "envelope_frames_inverted"),
        ({"canvas_multiple": 0}, "envelope_step_not_positive"),
        ({"grid_stride": 0}, "envelope_step_not_positive"),
        ({"short_edge": 500}, "envelope_edge_off_grid"),
        ({"min_frames": 34}, "envelope_frames_off_grid"),
    ],
)
def test_an_envelope_that_cannot_describe_a_request_is_refused(
    change: dict[str, object], code: str
) -> None:
    """Each construction invariant causes its own named rejection."""
    fields: dict[str, object] = {
        "model_set_id": OTHER_MODEL_SET,
        "short_edge": 512,
        "long_edge": 512,
        "min_frames": 33,
        "max_frames": 97,
        "grid_stride": 8,
        "grid_offset": 1,
        "canvas_multiple": 16,
        "source": "synthetic",
    }
    fields.update(change)

    with pytest.raises(TrainedEnvelopeDefinitionError) as raised:
        TrainedRange(**fields)  # type: ignore[arg-type]

    assert raised.value.code == code


def test_the_registry_reports_a_model_set_it_omits() -> None:
    """Read forward the registry cannot see an omission; the complement can.

    Driven with a population of one, which is the point of injecting the
    observed population rather than discovering it inside the check.
    """
    assert unregistered_model_sets(["minimax-h3-t2v-int8-20260809"]) == ()
    assert unregistered_model_sets([OTHER_MODEL_SET]) == (OTHER_MODEL_SET,)

    report = envelope_report([OTHER_MODEL_SET])

    assert report["unregistered_model_sets"] == (OTHER_MODEL_SET,)
    assert report["digest_verified"] is True


def test_observed_population_reads_the_published_catalogs(tmp_path: Path) -> None:
    """The doctor's observation is the model sets the repository publishes."""
    catalog_dir = tmp_path / "release-assets" / "some-set"
    catalog_dir.mkdir(parents=True)
    (catalog_dir / "catalog.json").write_text(
        json.dumps({"model_set_id": OTHER_MODEL_SET})
    )

    assert observed_model_sets(tmp_path) == (OTHER_MODEL_SET,)


def test_an_unreadable_catalog_contributes_nothing_rather_than_failing(
    tmp_path: Path,
) -> None:
    """A malformed catalog is the catalog validator's finding, not this scan's."""
    catalog_dir = tmp_path / "release-assets" / "broken"
    catalog_dir.mkdir(parents=True)
    (catalog_dir / "catalog.json").write_text("{not json")

    assert observed_model_sets(tmp_path) == ()


def test_the_repository_registers_every_model_set_it_publishes() -> None:
    """The shipped registry covers the real published population."""
    result = run_doctor()

    assert result.trained_envelope["unregistered_model_sets"] == ()
    assert result.trained_envelope["digest_verified"] is True
    assert not [error for error in result.errors if "envelope" in error]


def test_a_departure_reports_each_axis_independently() -> None:
    """Neither axis is inferred from the other."""
    narrow = OTHER_RANGE.departure(256, 256, OTHER_RANGE.min_frames)
    short = OTHER_RANGE.departure(
        OTHER_RANGE.long_edge, OTHER_RANGE.short_edge, OTHER_RANGE.grid_offset
    )

    assert narrow.short_edge_below_trained and not narrow.frames_below_trained
    assert short.frames_below_trained and not short.short_edge_below_trained
    assert EnvelopeDeparture(OTHER_MODEL_SET, False, False).inside
