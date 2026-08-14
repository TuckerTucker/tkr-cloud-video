"""Downstream implementation-boundary readiness tests."""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest
import yaml

from tkr_cloud_video.bootstrap import BOUNDARY_DIRECTORIES

ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "_tkr_kit"
GLOB_CHARACTERS: Final[str] = "*?["


def _declared_capabilities() -> set[str]:
    """Return every capability name declared in the plan."""
    document = yaml.safe_load((PLAN / "capabilities.yaml").read_text(encoding="utf-8"))
    return {entry["name"] for entry in document["capabilities"]}


def _observed_slice_shards() -> set[str]:
    """Return every capability that has a slice shard on disk."""
    return {path.stem for path in (PLAN / "slices").glob("*.yaml")}


def _slice_path_patterns() -> list[tuple[str, int, str]]:
    """Return every (capability, slice number, boundary pattern) in the plan."""
    patterns: list[tuple[str, int, str]] = []
    for shard in sorted((PLAN / "slices").glob("*.yaml")):
        document = yaml.safe_load(shard.read_text(encoding="utf-8"))
        for entry in document["slices"]:
            for pattern in entry.get("paths", ()):
                patterns.append((shard.stem, entry["number"], pattern))
    return patterns


def _glob_anchor(pattern: str) -> str:
    """Return the leading glob-free portion of a boundary pattern."""
    segments: list[str] = []
    for segment in pattern.split("/"):
        if any(character in segment for character in GLOB_CHARACTERS):
            break
        segments.append(segment)
    return "/".join(segments)


@pytest.mark.parametrize("relative", BOUNDARY_DIRECTORIES)
def test_declared_boundary_is_live_and_contained(relative: str) -> None:
    """Every downstream boundary has a live parent inside the checkout."""
    boundary = (ROOT / relative).resolve()

    assert boundary.is_dir()
    assert boundary.is_relative_to(ROOT.resolve())


def test_every_declared_capability_has_a_slice_shard() -> None:
    """The plan's capability list and its slice shards agree in both directions.

    Read forward this only asks whether each shard belongs to a capability, which
    cannot see a capability nobody sharded. Comparing the observed shard set against
    the declaration reports the omission instead of hiding it.
    """
    declared = _declared_capabilities()
    observed = _observed_slice_shards()

    assert declared - observed == set(), "capabilities declared with no slice shard"
    assert observed - declared == set(), "slice shards for undeclared capabilities"


def test_planned_targets_have_live_repository_parents() -> None:
    """Every slice boundary narrows from a live directory inside the checkout.

    The product's boundary rule holds that a slice's ``paths`` are live directory
    boundaries while its ``files`` may name targets not yet created. A boundary whose
    glob-free anchor matches nothing silently shrinks the writable scope rather than
    widening it, so an anchor that is not live is a defect.
    """
    patterns = _slice_path_patterns()
    assert patterns, "no slice boundaries were observed"

    dead = [
        (capability, number, pattern)
        for capability, number, pattern in patterns
        if not _anchor_is_live(pattern)
    ]

    assert dead == [], f"slice boundaries with no live anchor: {dead}"


def _anchor_is_live(pattern: str) -> bool:
    """Report whether a boundary pattern's glob-free anchor exists in the checkout."""
    anchor = _glob_anchor(pattern)
    if not anchor:
        return True
    target = (ROOT / anchor).resolve()
    if not target.is_relative_to(ROOT.resolve()):
        return False
    return target.is_dir() or target.is_file() or target.parent.is_dir()
