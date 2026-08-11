"""Offline doctor and dependency bootstrap tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.conftest import CapturingEventSink, FakeClock, FakeSettingsSource
from tkr_cloud_video.artifacts.publisher import PublicationReceipt
from tkr_cloud_video.bootstrap import (
    build_runtime,
    find_repository_root,
    run_doctor,
    run_runtime_doctor,
)
from tkr_cloud_video.cli import main, render_doctor
from tkr_cloud_video.core.settings import LogLevel

ROOT = Path(__file__).resolve().parents[2]


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


def test_serverless_cli_registers_only_async_handler(
    monkeypatch: object,
) -> None:
    """Long-lived startup runs on the pinned SDK handler event loop."""

    class Deployment:
        async def ensure_started(self) -> bool:
            return True

        async def handle(self, _event: object) -> dict[str, object]:
            return {"ok": True}

    class Sdk:
        config: dict[str, object] | None = None

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
    assert Module.serverless.config is not None
    assert "handler" in Module.serverless.config


def test_release_prepare_cli_writes_the_exact_manifest(
    tmp_path: Path, capsys: object
) -> None:
    """Preparation exposes safe identities and atomically writes canonical JSON."""
    output = tmp_path / "nested/model-set.json"

    assert (
        main(
            [
                "release",
                "prepare",
                "--catalog",
                str(ROOT / "release-assets/minimax-h3-t2v/catalog.json"),
                "--license-approval-id",
                "approval-1",
                "--output",
                str(output),
            ]
        )
        == 0
    )

    capture = capsys.readouterr()  # type: ignore[attr-defined]
    summary = json.loads(capture.out)
    manifest = json.loads(output.read_text())
    assert summary["model_set_id"] == manifest["model_set_id"]
    assert len(summary["manifest_digest"]) == 64
    assert not (output.parent / f".{output.name}.tmp").exists()


def test_release_prepare_cli_fails_safely_for_missing_catalog(
    tmp_path: Path, capsys: object
) -> None:
    """Preparation errors return one structured code without raw path details."""
    missing = tmp_path / "missing.json"

    assert (
        main(
            [
                "release",
                "prepare",
                "--catalog",
                str(missing),
                "--license-approval-id",
                "approval-1",
            ]
        )
        == 1
    )

    capture = capsys.readouterr()  # type: ignore[attr-defined]
    assert capture.out == ""
    assert "release_catalog_invalid" in capture.err
    assert str(missing) not in capture.err


def test_release_publish_cli_composes_streaming_store_without_exposing_secrets(
    tmp_path: Path, capsys: object, monkeypatch: object
) -> None:
    """Publication consumes env credentials and reports only immutable evidence."""

    class Publisher:
        def __init__(self, _store: object) -> None:
            pass

        async def publish(self, manifest: Any, sources: Any) -> PublicationReceipt:
            assert manifest.artifacts and sources
            return PublicationReceipt(
                "manifests/sha256/" + "a" * 64 + ".json",
                "a" * 64,
                "version-1",
                1,
                4,
            )

    monkeypatch.setattr(  # type: ignore[attr-defined]
        "tkr_cloud_video.cli.StreamingArtifactPublisher", Publisher
    )
    for name, value in {
        "TKR_B2_BUCKET_NAME": "private-bucket",
        "B2_MODEL_PUBLISHER_KEY_ID": "publisher-id",
        "B2_MODEL_PUBLISHER_APPLICATION_KEY": "publisher-value",
    }.items():
        monkeypatch.setenv(name, value)  # type: ignore[attr-defined]
    output = tmp_path / "model-set.json"

    assert (
        main(
            [
                "release",
                "publish",
                "--catalog",
                str(ROOT / "release-assets/minimax-h3-t2v/catalog.json"),
                "--license-approval-id",
                "approval-1",
                "--output",
                str(output),
            ]
        )
        == 0
    )

    capture = capsys.readouterr()  # type: ignore[attr-defined]
    result = json.loads(capture.out)
    assert result["uploaded_blobs"] == 4
    assert output.is_file()
    assert "publisher-id" not in capture.out + capture.err
    assert "publisher-value" not in capture.out + capture.err


def test_release_publish_cli_requires_offline_publisher_environment(
    capsys: object, monkeypatch: object
) -> None:
    """Missing publisher credentials fail before any object-store operation."""
    for name in (
        "TKR_B2_BUCKET_NAME",
        "B2_MODEL_PUBLISHER_KEY_ID",
        "B2_MODEL_PUBLISHER_APPLICATION_KEY",
    ):
        monkeypatch.delenv(name, raising=False)  # type: ignore[attr-defined]

    assert (
        main(
            [
                "release",
                "publish",
                "--catalog",
                str(ROOT / "release-assets/minimax-h3-t2v/catalog.json"),
                "--license-approval-id",
                "approval-1",
            ]
        )
        == 1
    )

    capture = capsys.readouterr()  # type: ignore[attr-defined]
    assert "release_environment_missing" in capture.err
