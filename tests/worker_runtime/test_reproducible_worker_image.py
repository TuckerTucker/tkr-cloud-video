"""Worker build manifest and OCI contract tests."""

from datetime import UTC, datetime
from pathlib import Path

from tkr_cloud_video.runtime.build_manifest import WorkerBuildManifest

ROOT = Path(__file__).resolve().parents[2]


def test_build_manifest_is_deterministic_and_secret_free() -> None:
    """Pinned build inputs produce a stable digest and sanitized model."""
    value = WorkerBuildManifest(
        image_digest="sha256:" + "a" * 64,
        python_lock_digest="b" * 64,
        comfyui_revision="c" * 40,
        custom_node_revisions={"node": "d" * 40},
        tool_versions={"rclone": "1.1"},
        built_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert value.digest() == value.model_copy().digest()
    assert "secret" not in value.model_dump_json().lower()


def test_dockerfile_pins_gpu_comfy_and_runtime_tools() -> None:
    """Image contract includes immutable CUDA, ComfyUI, and required tools."""
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04@sha256:" in dockerfile
    assert "COMFYUI_REVISION=dec5d9450a5290bcf63430409ea41018e67f41c3" in dockerfile
    assert "RCLONE_RELEASE=1.75.0" in dockerfile
    assert "ARG RCLONE_VERSION" not in dockerfile
    assert "RCLONE_SHA256=aa2804e08f48250e71009c727124b634" in dockerfile
    assert (
        "ca-certificates curl ffmpeg git python3.12 python3.12-venv unzip" in dockerfile
    )
    assert "uv sync --frozen --no-dev" in dockerfile
    assert "--require-hashes --torch-backend cu128" in dockerfile
    assert "requirements/comfyui.lock" in dockerfile
    comfy_lock = (ROOT / "requirements" / "comfyui.lock").read_text()
    assert "torch==2.11.0+cu128" in comfy_lock
    assert "cuda-toolkit==12.8.1" in comfy_lock
    assert "USER 65532:65532" in dockerfile
    assert "useradd --uid 65532 --gid 65532" in dockerfile
    assert "RUNPOD_SECRET" not in dockerfile
    entrypoint = (ROOT / "scripts" / "entrypoint.sh").read_text()
    assert "set -- serverless" in entrypoint
