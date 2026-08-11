"""Production worker composition and fail-closed lifecycle runner."""

from __future__ import annotations

import asyncio
import json
import shutil
import signal
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tkr_cloud_video.adapters.comfy import (
    ComfyApiClient,
    PollingComfyApi,
    UrllibJsonTransport,
)
from tkr_cloud_video.adapters.process import SubprocessCommandExecutor
from tkr_cloud_video.adapters.rclone import (
    ArtifactRcloneStore,
    RcloneB2Client,
    RcloneBlobDownloader,
    RcloneCredentials,
    RcloneLocation,
)
from tkr_cloud_video.adapters.runtime import (
    ComfyProcessRunner,
    EnvironmentSecretResolver,
    LocalPlatformProbe,
)
from tkr_cloud_video.artifacts.cache import CacheIndex, CachePolicy
from tkr_cloud_video.artifacts.hydrator import ArtifactHydrator
from tkr_cloud_video.artifacts.materializer import ModelMaterializer
from tkr_cloud_video.artifacts.models import ArtifactRole, ModelSetManifest
from tkr_cloud_video.artifacts.publisher import ArtifactStore
from tkr_cloud_video.artifacts.resolver import ApprovedReleaseResolver
from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.core.logging import EventSink, configure_logging
from tkr_cloud_video.runtime.config import WorkerSettings, load_worker_settings
from tkr_cloud_video.runtime.preflight import WorkerPreflight
from tkr_cloud_video.security.process_secrets import (
    ProcessEnvironmentBuilder,
    ProcessRole,
    SecretReference,
)
from tkr_cloud_video.security.validation import (
    ObjectKey,
    RelativeDestination,
    Sha256Digest,
)
from tkr_cloud_video.worker_runtime.composition import (
    WorkerDependencies,
    WorkerServices,
    compose_worker_runtime,
)


@dataclass(frozen=True, slots=True)
class HydratedWorkflow:
    """Digest-pinned workflow bytes and its approved binding contract."""

    name: str
    content: bytes
    sha256: Sha256Digest
    bindings: tuple[tuple[str, str, str], ...]


class WorkerStartupSteps:
    """Resolve, preflight, hydrate, and materialize one approved model set."""

    def __init__(
        self,
        settings: WorkerSettings,
        store: ArtifactStore,
        preflight: WorkerPreflight,
        hydrator: ArtifactHydrator,
        materializer: ModelMaterializer,
    ) -> None:
        """Initialize with explicit storage and local-cache dependencies."""
        self._settings = settings
        self._resolver = ApprovedReleaseResolver(store)
        self._preflight = preflight
        self._hydrator = hydrator
        self._materializer = materializer
        self._manifest: ModelSetManifest | None = None
        self._workflow_documents: list[dict[str, Any]] = []
        self._workflows: dict[str, HydratedWorkflow] = {}

    async def configure(self) -> None:
        """Validate immutable local paths before any remote operation."""
        roots = (
            self._settings.cache_root,
            self._settings.model_root,
            self._settings.workspace_root,
            self._settings.output_root,
            self._settings.comfyui_root,
        )
        if any(not root.is_absolute() or not root.is_dir() for root in roots):
            raise AppError(
                "worker_directory_invalid",
                "A configured worker directory is unavailable.",
            )
        if not (self._settings.comfyui_root / "main.py").is_file():
            raise AppError("comfy_entrypoint_missing", "ComfyUI entrypoint is absent.")

    async def preflight(self) -> None:
        """Resolve exact manifest evidence, then prove capacity and authorization."""
        manifest = await self._resolver.resolve(
            ObjectKey(self._settings.manifest_key),
            Sha256Digest(self._settings.manifest_digest),
        )
        if manifest.model_set_id != self._settings.model_set_id:
            raise AppError(
                "model_set_identity_mismatch",
                "Manifest model-set identity differs from configuration.",
            )
        self._manifest = manifest
        await self._preflight.run(
            self._settings, sum(entry.size_bytes for entry in manifest.artifacts)
        )

    async def hydrate(self) -> None:
        """Hydrate every verified blob and atomically materialize exact destinations."""
        manifest = self._require_manifest()
        for entry in manifest.artifacts:
            blob, _downloaded = await self._hydrator.hydrate(entry)
            path = self._materializer.materialize(
                blob, RelativeDestination(entry.destination)
            )
            if entry.role is ArtifactRole.WORKFLOW:
                content = path.read_bytes()
                self._workflow_documents.append(_workflow_document(path))
                self._workflows[entry.name] = HydratedWorkflow(
                    entry.name,
                    content,
                    Sha256Digest(entry.sha256),
                    tuple(
                        (
                            binding.source.value,
                            binding.node_id,
                            binding.input_name,
                        )
                        for binding in entry.bindings
                    ),
                )

    def required_nodes(self) -> frozenset[str]:
        """Return node classes referenced by the hydrated workflow documents."""
        return frozenset(
            class_type
            for workflow in self._workflow_documents
            for node in workflow.values()
            if isinstance(node, dict)
            for class_type in [node.get("class_type")]
            if isinstance(class_type, str)
        )

    def required_models(self) -> frozenset[str]:
        """Return exact model basenames declared by the approved manifest."""
        return frozenset(
            Path(entry.destination).name
            for entry in self._require_manifest().artifacts
            if entry.role is ArtifactRole.MODEL
        )

    def workflow(self, workflow_id: str) -> HydratedWorkflow:
        """Return one approved hydrated workflow or fail before GPU submission."""
        workflow = self._workflows.get(workflow_id)
        if workflow is None:
            raise AppError(
                "workflow_not_approved",
                "Requested workflow is not present in the approved model set.",
            )
        if not workflow.bindings:
            raise AppError(
                "workflow_bindings_missing",
                "Approved workflow does not declare parameter bindings.",
            )
        return workflow

    def _require_manifest(self) -> ModelSetManifest:
        if self._manifest is None:
            raise AppError(
                "manifest_unavailable",
                "Approved manifest is unavailable for startup.",
            )
        return self._manifest


@dataclass(frozen=True, slots=True)
class WorkerApplication:
    """Fully composed worker runtime and its sanitized settings."""

    settings: WorkerSettings
    services: WorkerServices
    startup: WorkerStartupSteps
    comfy_client: ComfyApiClient
    comfy_environment: dict[str, str]

    async def run(self, stop: asyncio.Event | None = None) -> None:
        """Become ready, wait for termination, then drain the owned child."""
        stop_event = stop or asyncio.Event()
        _install_signal_handlers(stop_event)
        await self.services.supervisor.start(self.comfy_environment)
        await stop_event.wait()
        await self.services.supervisor.drain(self.settings.shutdown_timeout_seconds)


def compose_worker_application(
    environment: Mapping[str, str], event_sink: EventSink
) -> WorkerApplication:
    """Compose the deployable worker exclusively from explicit adapters."""
    settings = load_worker_settings(environment)
    logger = configure_logging(event_sink)
    resolver = EnvironmentSecretResolver(environment)
    environments = ProcessEnvironmentBuilder(resolver)
    hydrator_environment = environments.build(
        ProcessRole.HYDRATOR,
        {},
        (
            SecretReference("B2_MODEL_KEY_ID", settings.model_key_id_secret_reference),
            SecretReference(
                "B2_MODEL_APPLICATION_KEY",
                settings.model_application_key_secret_reference,
            ),
        ),
    )
    credentials = RcloneCredentials(
        hydrator_environment["B2_MODEL_KEY_ID"],
        hydrator_environment["B2_MODEL_APPLICATION_KEY"],
    )
    client = RcloneB2Client(
        SubprocessCommandExecutor(),
        credentials,
        RcloneLocation(
            settings.rclone_remote_name,
            settings.bucket_name,
            settings.model_prefix,
        ),
    )
    store = ArtifactRcloneStore(client)
    filesystem_bytes = shutil.disk_usage(settings.cache_root).total
    cache_capacity = max(1, filesystem_bytes - settings.disk_safety_bytes)
    cache = CacheIndex(
        settings.cache_root,
        CachePolicy(cache_capacity, max(1, cache_capacity * 9 // 10)),
    )
    probe = LocalPlatformProbe(settings.cache_root, client)
    preflight = WorkerPreflight(probe)
    startup = WorkerStartupSteps(
        settings,
        store,
        preflight,
        ArtifactHydrator(cache, RcloneBlobDownloader(client)),
        ModelMaterializer(settings.model_root),
    )
    comfy_client = ComfyApiClient(
        UrllibJsonTransport(f"http://127.0.0.1:{settings.comfyui_port}")
    )
    comfy_api = PollingComfyApi(comfy_client, settings.startup_timeout_seconds)
    runner = ComfyProcessRunner(
        settings.python_executable,
        settings.comfyui_root / "main.py",
        settings.workspace_root,
        settings.output_root,
        port=settings.comfyui_port,
        user_root=settings.workspace_root / ".comfy-user",
        temp_root=settings.workspace_root / ".comfy-temp",
    )
    services = compose_worker_runtime(
        WorkerDependencies(probe, runner, startup, comfy_api)
    )
    comfy_environment = environments.build(
        ProcessRole.COMFYUI,
        {
            "HOME": str(settings.workspace_root),
            "HF_HOME": str(settings.cache_root / "huggingface"),
            "PATH": "/opt/venv/bin:/usr/bin:/bin",
            "PYTHONUNBUFFERED": "1",
            "TORCH_HOME": str(settings.cache_root / "torch"),
            "TORCHINDUCTOR_CACHE_DIR": str(settings.cache_root / "torchinductor"),
            "XDG_CACHE_HOME": str(settings.cache_root),
        },
    )
    logger.info(
        "worker.composition.succeeded",
        correlation_id=settings.worker_id,
        release_id=settings.release_id,
        worker_id=settings.worker_id,
        outcome="succeeded",
    )
    return WorkerApplication(
        settings, services, startup, comfy_client, comfy_environment
    )


def _workflow_document(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AppError(
            "workflow_invalid",
            "Approved workflow is not valid JSON.",
            cause=error,
        ) from error
    if not isinstance(value, dict):
        raise AppError("workflow_invalid", "Approved workflow must be an object.")
    return value


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_name, stop.set)
        except NotImplementedError:
            continue
