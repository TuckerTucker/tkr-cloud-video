"""Production worker startup composition and lifecycle tests."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.artifact_foundation.test_immutable_artifact_catalog import MemoryStore

from tkr_cloud_video.artifacts.cache import CacheIndex, CachePolicy
from tkr_cloud_video.artifacts.hydrator import ArtifactHydrator
from tkr_cloud_video.artifacts.materializer import ModelMaterializer
from tkr_cloud_video.artifacts.models import (
    ArtifactEntry,
    ArtifactRole,
    ModelSetManifest,
    WorkflowBinding,
    WorkflowBindingSource,
)
from tkr_cloud_video.artifacts.publisher import ObjectMetadata
from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.runtime.config import WorkerSettings, load_worker_settings
from tkr_cloud_video.runtime.preflight import WorkerPreflight
from tkr_cloud_video.worker import WorkerStartupSteps


@dataclass
class Downloader:
    """Object-keyed streaming downloader fake."""

    content: dict[str, bytes]

    async def download(self, object_key: str, destination: Path) -> None:
        """Write exact configured bytes to a new partial file."""
        destination.write_bytes(self.content[object_key])


@dataclass
class Probe:
    """Passing worker capacity probe."""

    def gpu_available(self) -> bool:
        return True

    def available_disk_bytes(self) -> int:
        return 10_000_000

    async def storage_authorized(self) -> bool:
        return True


def artifact(name: str, role: ArtifactRole, content: bytes) -> ArtifactEntry:
    """Build one digest-addressed startup artifact."""
    digest = hashlib.sha256(content).hexdigest()
    extension = "json" if role is ArtifactRole.WORKFLOW else "safetensors"
    destination = (
        f"workflows/{name}.{extension}"
        if role is ArtifactRole.WORKFLOW
        else f"checkpoints/{name}.{extension}"
    )
    return ArtifactEntry(
        name=name,
        role=role,
        object_key=f"blobs/sha256/{digest}",
        sha256=digest,
        size_bytes=len(content),
        destination=destination,
        bindings=(
            WorkflowBinding(
                source=WorkflowBindingSource.PROMPT,
                node_id="10",
                input_name="text",
            ),
        )
        if role is ArtifactRole.WORKFLOW
        else (),
    )


def startup_fixture(tmp_path: Path) -> tuple[WorkerStartupSteps, WorkerSettings]:
    """Compose startup against complete immutable in-memory evidence."""
    workflow = json.dumps(
        {"10": {"class_type": "H3Sampler", "inputs": {"model": "h3"}}}
    ).encode()
    content = {"model-1": b"model-bytes", "workflow-1": workflow}
    entries = (
        artifact("model-1", ArtifactRole.MODEL, content["model-1"]),
        artifact("workflow-1", ArtifactRole.WORKFLOW, content["workflow-1"]),
    )
    manifest = ModelSetManifest(
        model_set_id="models-1",
        created_at=datetime(2026, 8, 10, tzinfo=UTC),
        provenance_digest="a" * 64,
        license_approval_id="approval-1",
        artifacts=entries,
    )
    manifest_content = manifest.canonical_bytes()
    manifest_digest = hashlib.sha256(manifest_content).hexdigest()
    manifest_key = f"manifests/sha256/{manifest_digest}.json"
    store = MemoryStore()
    store.objects[manifest_key] = manifest_content
    store.metadata[manifest_key] = ObjectMetadata(
        len(manifest_content), manifest_digest, "version-manifest"
    )
    for entry in entries:
        store.objects[entry.object_key] = content[entry.name]
        store.metadata[entry.object_key] = ObjectMetadata(
            entry.size_bytes, entry.sha256, f"version-{entry.name}"
        )
    roots = {
        name: tmp_path / name for name in ("cache", "models", "work", "out", "comfy")
    }
    for root in roots.values():
        root.mkdir()
    (roots["comfy"] / "main.py").write_text("# pinned\n")
    settings = WorkerSettings(
        release_id="release-1",
        worker_id="worker-1",
        model_set_id="models-1",
        manifest_digest=manifest_digest,
        bucket_name="private-bucket",
        cache_root=roots["cache"],
        model_root=roots["models"],
        workspace_root=roots["work"],
        output_root=roots["out"],
        comfyui_root=roots["comfy"],
        disk_safety_bytes=0,
    )
    cache = CacheIndex(roots["cache"], CachePolicy(1_000_000, 900_000))
    steps = WorkerStartupSteps(
        settings,
        store,
        WorkerPreflight(Probe()),
        ArtifactHydrator(
            cache,
            Downloader({entry.object_key: content[entry.name] for entry in entries}),
        ),
        ModelMaterializer(roots["models"]),
    )
    return steps, settings


@pytest.mark.asyncio
async def test_startup_resolves_preflights_hydrates_and_derives_inventory(
    tmp_path: Path,
) -> None:
    """The production sequence reconstructs only the pinned approved runtime."""
    steps, settings = startup_fixture(tmp_path)

    await steps.configure()
    await steps.preflight()
    await steps.hydrate()

    assert steps.required_nodes() == frozenset({"H3Sampler"})
    assert steps.required_models() == frozenset({"model-1.safetensors"})
    assert steps.workflow("workflow-1").bindings[0][0] == "prompt"
    assert (settings.model_root / "checkpoints/model-1.safetensors").is_symlink()
    assert (settings.model_root / "workflows/workflow-1.json").is_symlink()


@pytest.mark.asyncio
async def test_startup_fails_before_remote_or_hydration_when_local_runtime_is_absent(
    tmp_path: Path,
) -> None:
    """Missing ComfyUI entrypoint remains unready and performs no hydration."""
    steps, settings = startup_fixture(tmp_path)
    (settings.comfyui_root / "main.py").unlink()

    with pytest.raises(AppError, match="entrypoint"):
        await steps.configure()
    with pytest.raises(AppError, match="unavailable"):
        steps.required_models()


def test_worker_environment_is_allowlisted_and_validates_prefix() -> None:
    """Unknown ambient data is ignored while required identities fail closed."""
    environment = {
        "TKR_RELEASE_ID": "release-1",
        "TKR_WORKER_ID": "worker-1",
        "TKR_MODEL_SET_ID": "models-1",
        "TKR_MANIFEST_DIGEST": "a" * 64,
        "TKR_B2_BUCKET_NAME": "private-bucket",
        "UNRELATED": "ignored",
    }
    settings = load_worker_settings(environment)

    assert settings.manifest_key.endswith("a" * 64 + ".json")
    assert "UNRELATED" not in settings.model_dump()
    assert settings.sanitized()["release_id"] == "release-1"
    with pytest.raises(ValueError):
        load_worker_settings({**environment, "TKR_MODEL_PREFIX": "../escape/"})
