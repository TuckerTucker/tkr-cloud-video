"""Tests binding configured provider placement to the ratified licence record.

The deployment scripts name data centres; the licence approval names
territories. Nothing in the running code connects the two: ``ReleaseAssuranceGate``
checks territory but has no runtime caller, and the provider omits
``dataCenterIds`` from its REST responses. Until a runtime caller exists, these
tests are the only thing that fails when a script starts placing workers
somewhere the reviewer never approved.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tkr_cloud_video.security.release_gate import LicenseApproval
from tkr_cloud_video.security.territories import (
    LICENSE_EXCLUDED_TERRITORIES,
    review_placement,
)

ROOT = Path(__file__).resolve().parents[2]
APPROVAL_PATH = ROOT / "release-assets/minimax-h3-t2v/license-approval.json"
SCRIPTS = ROOT / "scripts"

# Placement reaches the provider as a JSON array of region identifiers, but the
# scripts build that array from a shell variable so the create path and the
# repair path cannot drift apart. Both spellings are scanned: the variable is
# where the regions are written today, and the inline array is what a future
# script would most likely reach for. Matching the text rather than executing
# the script keeps this independent of credentials and of the provider being up.
PLACEMENT_SOURCES = (
    re.compile(r"DATA_CENTRE_IDS=([^\n]*)"),
    re.compile(r'"dataCenterIds"\s*:\s*\[([^\]]*)\]'),
)

# Region identifiers are three segments: a continent or country prefix, a city,
# state or country segment, and an ordinal. Matching that shape rather than any
# quoted token keeps shell variable names and JSON keys out of the results.
REGION_LITERAL = re.compile(r"\b([A-Z]{2,3}-[A-Z]{2,3}-[0-9]+)\b")


def ratified_approval() -> LicenseApproval:
    """Return the ratified approval, or fail the run if it is not ratified."""
    return LicenseApproval.model_validate(json.loads(APPROVAL_PATH.read_text()))


def configured_placements() -> dict[str, tuple[str, ...]]:
    """Return the regions each script configures, keyed by script name."""
    placements: dict[str, tuple[str, ...]] = {}
    for script in sorted(SCRIPTS.glob("*.sh")):
        text = script.read_text()
        regions: list[str] = []
        for pattern in PLACEMENT_SOURCES:
            for body in pattern.findall(text):
                regions.extend(REGION_LITERAL.findall(body))
        if regions:
            # Duplicates are meaningless here: the same region named twice is
            # still one place a worker may run.
            placements[script.name] = tuple(sorted(set(regions)))
    return placements


def test_a_script_configures_placement_at_all() -> None:
    """Guard the regex: a silent zero-match would make every check below vacuous."""
    assert configured_placements(), (
        "no script sets dataCenterIds; either placement moved or the pattern "
        "stopped matching, and both make these tests pass without checking"
    )


@pytest.mark.parametrize("script", sorted(configured_placements()))
def test_configured_regions_sit_inside_the_approved_territories(script: str) -> None:
    """Every region a script places into must map into an approved territory.

    This is the check ADR-001 asks for. Adding a data centre in an excluded
    state, or one absent from the registry, becomes a territory decision that
    fails here rather than a config edit that widens where the model is served.
    """
    approval = ratified_approval()
    review = review_placement(configured_placements()[script], approval.territories)
    assert review.approved, (
        f"{script} places outside the grant "
        f"{','.join(approval.territories)}: "
        f"excluded={','.join(review.excluded) or 'none'} "
        f"unregistered={','.join(review.unmapped) or 'none'}"
    )


def test_the_approval_covers_no_licence_excluded_territory() -> None:
    """The grant must lapse by itself when the licence reading narrows.

    The plan for IS and NO is that licensing drops them when the licensor
    answers on the EEA. That only happens on its own if narrowing the exclusion
    set breaks something: the record is a static file, and the placement check
    reads its territories without ever asking whether the licence still permits
    them. So an EEA answer would otherwise leave workers running in Iceland and
    Norway under a grant nobody re-read.

    Moving a territory into LICENSE_EXCLUDED_TERRITORIES now fails here, and the
    fix is to narrow the record, which narrows placement on its next run.
    """
    approval = ratified_approval()
    excluded = sorted(set(approval.territories) & LICENSE_EXCLUDED_TERRITORIES)
    assert not excluded, (
        f"{APPROVAL_PATH.name} grants territories the licence excludes: "
        f"{','.join(excluded)}. Narrow the record's territories, then re-run "
        f"the placement scripts so the endpoint stops placing there."
    )


def test_the_approval_record_on_disk_is_ratified() -> None:
    """A draft or malformed record must not sit where scripts read a grant.

    ``scripts/runpod_graphql.sh`` reads its territories from this file, so an
    unratified record there is not an inert document: it is the input to the
    only check that confirms placement.
    """
    approval = ratified_approval()
    assert approval.territories
    assert approval.reviewer_id
    assert approval.expires_at > approval.approved_at
