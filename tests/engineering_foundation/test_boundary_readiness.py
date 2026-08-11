"""Downstream implementation-boundary readiness tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from tkr_cloud_video.bootstrap import BOUNDARY_DIRECTORIES

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("relative", BOUNDARY_DIRECTORIES)
def test_declared_boundary_is_live_and_contained(relative: str) -> None:
    """Every downstream boundary has a live parent inside the checkout."""
    boundary = (ROOT / relative).resolve()

    assert boundary.is_dir()
    assert boundary.is_relative_to(ROOT.resolve())


def test_planned_targets_have_live_repository_parents() -> None:
    """Every canonical downstream file target can be narrowed from the live root."""
    slice_files = sorted((ROOT / "_tkr_kit" / "slices").glob("*.yaml"))

    assert len(slice_files) == 7
    assert all(
        path.parent.is_dir() and path.is_relative_to(ROOT) for path in slice_files
    )
