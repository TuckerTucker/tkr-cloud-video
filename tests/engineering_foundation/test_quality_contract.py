"""Contract tests for the single local and CI quality command."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_quality_gate_order_and_non_mutating_commands() -> None:
    """The script owns a fixed fail-fast check-only gate sequence."""
    script = (ROOT / "scripts" / "check.sh").read_text()
    expected = (
        "lock_check",
        "format_check",
        "lint",
        "type_check",
        "unit_test",
        "package_smoke",
        "doctor",
    )
    offsets = [script.index(f"run_gate {gate}") for gate in expected]

    assert offsets == sorted(offsets)
    assert "set -euo pipefail" in script
    assert "ruff format --check" in script
    assert "uv lock --check" in script


def test_ci_delegates_policy_with_read_only_permissions() -> None:
    """CI synchronizes frozen dependencies and invokes the checked-in script."""
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()

    assert "contents: read" in workflow
    assert "uv sync --frozen --all-groups" in workflow
    assert "./scripts/check.sh --ci" in workflow
    assert "secrets." not in workflow
