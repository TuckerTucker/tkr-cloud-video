"""Static contracts for the reproducible project foundation."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_required_release_inputs_exist() -> None:
    """Packaging metadata, lock data, and typed marker are committed inputs."""
    required = (
        "pyproject.toml",
        "uv.lock",
        "README.md",
        "src/tkr_cloud_video/__init__.py",
        "src/tkr_cloud_video/py.typed",
    )
    assert all((ROOT / relative).is_file() for relative in required)


def test_package_import_has_no_provider_side_effects() -> None:
    """Import succeeds without loading a cloud or HTTP provider SDK."""
    process = subprocess.run(  # noqa: S603 - fixed interpreter and literal arguments.
        [
            sys.executable,
            "-c",
            (
                "import sys, tkr_cloud_video; "
                "assert not any(name.startswith(('boto', 'runpod', 'httpx')) "
                "for name in sys.modules)"
            ),
        ],
        cwd=ROOT.parent,
        check=False,
        capture_output=True,
        text=True,
    )
    assert process.returncode == 0, process.stderr
