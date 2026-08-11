"""Offline doctor and dependency bootstrap tests."""

from __future__ import annotations

import json
from pathlib import Path

from tests.conftest import CapturingEventSink, FakeClock, FakeSettingsSource
from tkr_cloud_video.bootstrap import (
    build_runtime,
    find_repository_root,
    run_doctor,
    run_runtime_doctor,
)
from tkr_cloud_video.cli import main, render_doctor
from tkr_cloud_video.core.settings import LogLevel


def test_runtime_bootstrap_uses_only_injected_adapters() -> None:
    """Composition returns the caller-owned clock, source data, and sink."""
    clock = FakeClock()
    sink = CapturingEventSink()
    runtime = build_runtime(
        FakeSettingsSource({"ENVIRONMENT": "test", "LOG_LEVEL": "WARNING"}),
        clock,
        sink,
    )

    assert runtime.clock is clock
    assert runtime.event_sink is sink
    assert runtime.settings.log_level is LogLevel.WARNING


def test_doctor_succeeds_for_repository() -> None:
    """A valid checkout passes all local checks without provider state."""
    result = run_doctor()

    assert result.outcome == "succeeded"
    assert result.context == "repository"
    assert result.lock_digest is not None
    assert len(result.lock_digest) == 64
    assert all(result.boundary_directories.values())
    assert result.errors == ()


def test_doctor_reports_missing_local_contracts(tmp_path: Path) -> None:
    """Missing lock/core/boundaries are independent safe failures."""
    result = run_doctor(tmp_path)

    assert result.outcome == "failed"
    assert result.lock_digest is None
    assert "lock_missing" in result.errors
    assert "core_contract_missing" in result.errors
    assert any(error.startswith("missing_boundary:") for error in result.errors)


def test_doctor_succeeds_for_installed_runtime(tmp_path: Path) -> None:
    """An installed worker validates package files without source-only boundaries."""
    (tmp_path / "uv.lock").write_text("version = 1\n")

    result = run_runtime_doctor(tmp_path)

    assert result.outcome == "succeeded"
    assert result.context == "runtime"
    assert result.core_contracts
    assert result.boundary_directories == {}
    assert result.lock_digest is not None


def test_runtime_doctor_requires_embedded_lock(tmp_path: Path) -> None:
    """A packaged worker fails closed when its reviewed lock is absent."""
    result = run_runtime_doctor(tmp_path)

    assert result.outcome == "failed"
    assert result.context == "runtime"
    assert result.errors == ("lock_missing",)


def test_doctor_human_and_json_cli(capsys: object) -> None:
    """Both output modes use the same result schema and exit successfully."""
    assert main(["doctor"]) == 0
    capture = capsys.readouterr()  # type: ignore[attr-defined]
    assert "doctor: OK" in capture.out

    assert main(["doctor", "--json"]) == 0
    capture = capsys.readouterr()  # type: ignore[attr-defined]
    payload = json.loads(capture.out)
    assert payload["outcome"] == "succeeded"


def test_failed_doctor_render_is_contextual(tmp_path: Path) -> None:
    """Human diagnostics name missing relative boundaries, never absolute roots."""
    result = run_doctor(tmp_path)
    rendered = render_doctor(result, as_json=False)

    assert "FAILED" in rendered
    assert "src/tkr_cloud_video" in rendered
    assert str(tmp_path) not in rendered


def test_repository_root_discovery_from_nested_path() -> None:
    """Root discovery walks upward from a nested injected path."""
    root = find_repository_root(Path(__file__))
    assert (root / "pyproject.toml").is_file()


def test_serverless_cli_registers_fitness_and_async_handler(
    monkeypatch: object,
) -> None:
    """The production command delegates startup and handling to the pinned SDK."""

    class Deployment:
        async def ensure_started(self) -> bool:
            return True

        async def handle(self, _event: object) -> dict[str, object]:
            return {"ok": True}

    class Sdk:
        check: object | None = None
        config: dict[str, object] | None = None

        def register_fitness_check(self, check: object) -> object:
            self.check = check
            return check

        def start(self, config: dict[str, object]) -> None:
            self.config = config

    class Module:
        serverless = Sdk()

    monkeypatch.setattr(  # type: ignore[attr-defined]
        "tkr_cloud_video.cli.compose_serverless_deployment",
        lambda *_args: Deployment(),
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        "tkr_cloud_video.cli.importlib.import_module", lambda _name: Module()
    )

    assert main(["serverless"]) == 0
    assert Module.serverless.check is not None
    assert Module.serverless.config is not None
    assert "handler" in Module.serverless.config
