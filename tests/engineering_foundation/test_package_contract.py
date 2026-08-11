"""Distribution metadata and locked runtime dependency contract tests."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_distribution_has_one_typed_package_and_deployable_entrypoint() -> None:
    """Build metadata exposes the typed package through one stable CLI command."""
    configuration = tomllib.loads((ROOT / "pyproject.toml").read_text())

    assert configuration["project"]["scripts"] == {
        "tkr-cloud-video": "tkr_cloud_video.cli:main"
    }
    assert configuration["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/tkr_cloud_video"
    ]
    assert (ROOT / "src/tkr_cloud_video/py.typed").is_file()


def test_runtime_dependencies_are_constrained_and_locked_once() -> None:
    """Runtime requirements are bounded in project metadata and resolved by uv."""
    configuration = tomllib.loads((ROOT / "pyproject.toml").read_text())
    dependencies = configuration["project"]["dependencies"]
    lock = (ROOT / "uv.lock").read_text()

    assert dependencies == [
        "pydantic>=2.12.5,<3",
        "runpod==1.11.0",
        "structlog>=25.5.0,<26",
    ]
    assert lock.count('name = "runpod"') >= 1
    assert "requirements.txt" not in configuration
