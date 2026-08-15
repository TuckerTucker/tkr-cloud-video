"""Deployable RunPod Serverless composition over the shared application."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import asdict, dataclass

from tkr_cloud_video.adapters.media import (
    FfprobeInputInspector,
    FfprobeMediaInspector,
)
from tkr_cloud_video.adapters.process import SubprocessCommandExecutor
from tkr_cloud_video.adapters.rclone import (
    RcloneB2Client,
    RcloneCredentials,
    RcloneInputSource,
    RcloneLocation,
    ResultRcloneStore,
)
from tkr_cloud_video.adapters.runtime import (
    AsyncioWaiter,
    EnvironmentSecretResolver,
)
from tkr_cloud_video.application import CloudVideoApplication
from tkr_cloud_video.core.clock import SystemClock
from tkr_cloud_video.core.logging import EventSink
from tkr_cloud_video.job_execution.composition import (
    JobDependencies,
    compose_job_execution,
)
from tkr_cloud_video.prompt_authoring.composition import compose_prompt_authoring
from tkr_cloud_video.release.runpod_handler import HandlerResponse, RunPodHandler
from tkr_cloud_video.release.runpod_runtime import (
    RunPodRuntimeDiagnostic,
    RunPodRuntimeDiagnosticPublisher,
)
from tkr_cloud_video.security.process_secrets import (
    ProcessEnvironmentBuilder,
    ProcessRole,
    SecretReference,
)
from tkr_cloud_video.worker import WorkerApplication, compose_worker_application


@dataclass
class ServerlessDeployment:
    """Start one worker on the SDK job loop, then serve the typed handler."""

    worker: WorkerApplication
    handler: RunPodHandler
    runtime_diagnostic: RunPodRuntimeDiagnosticPublisher | None = None

    def __post_init__(self) -> None:
        """Create a concurrency-safe one-time startup gate."""
        self._startup_lock = asyncio.Lock()

    async def ensure_started(self) -> bool:
        """Hydrate and validate once on the long-lived SDK job event loop."""
        async with self._startup_lock:
            if not self.worker.services.lifecycle.ready:
                await self.worker.services.supervisor.start(
                    self.worker.comfy_environment
                )
        return self.worker.services.lifecycle.ready

    async def handle(self, event: object) -> dict[str, object]:
        """Reject invalid input before startup, then run the canonical handler."""
        if self.runtime_diagnostic is not None:
            await self.runtime_diagnostic.publish()
        validated = self.handler.validate(event)
        if isinstance(validated, HandlerResponse):
            return asdict(validated)
        await self.ensure_started()
        return asdict(await self.handler.handle_request(validated))


def compose_serverless_deployment(
    environment: Mapping[str, str], event_sink: EventSink
) -> ServerlessDeployment:
    """Compose model, input, output, ComfyUI, and RunPod boundaries."""
    worker = compose_worker_application(environment, event_sink)
    settings = worker.settings
    resolver = EnvironmentSecretResolver(environment)
    environments = ProcessEnvironmentBuilder(resolver)
    input_environment = environments.build(
        ProcessRole.INPUT_READER,
        {},
        (
            SecretReference("B2_INPUT_KEY_ID", settings.input_key_id_secret_reference),
            SecretReference(
                "B2_INPUT_APPLICATION_KEY",
                settings.input_application_key_secret_reference,
            ),
        ),
    )
    output_environment = environments.build(
        ProcessRole.UPLOADER,
        {},
        (
            SecretReference(
                "B2_OUTPUT_KEY_ID", settings.output_key_id_secret_reference
            ),
            SecretReference(
                "B2_OUTPUT_APPLICATION_KEY",
                settings.output_application_key_secret_reference,
            ),
        ),
    )
    executor = SubprocessCommandExecutor()
    input_client = RcloneB2Client(
        executor,
        RcloneCredentials(
            input_environment["B2_INPUT_KEY_ID"],
            input_environment["B2_INPUT_APPLICATION_KEY"],
        ),
        RcloneLocation(
            settings.rclone_remote_name,
            settings.bucket_name,
            settings.input_prefix,
            settings.b2_s3_endpoint,
            settings.b2_s3_region,
        ),
    )
    output_client = RcloneB2Client(
        executor,
        RcloneCredentials(
            output_environment["B2_OUTPUT_KEY_ID"],
            output_environment["B2_OUTPUT_APPLICATION_KEY"],
        ),
        RcloneLocation(
            settings.rclone_remote_name,
            settings.bucket_name,
            settings.output_prefix,
            settings.b2_s3_endpoint,
            settings.b2_s3_region,
        ),
    )
    media = FfprobeMediaInspector()
    clock = SystemClock()
    jobs = compose_job_execution(
        JobDependencies(
            clock,
            AsyncioWaiter(),
            worker.comfy_client,
            RcloneInputSource(input_client),
            FfprobeInputInspector(media),
            media,
            settings.workspace_root,
            settings.maximum_input_bytes,
        )
    )
    result_store = ResultRcloneStore(output_client)
    # One boundary, shared by the handler's preflight and the execution path
    # that binds the wire text. Composing it twice would let a future caller
    # narrow one check set without the other, so the preflight could accept a
    # prompt the binder then refuses - after the cold start it exists to avoid.
    prompts = compose_prompt_authoring()
    application = CloudVideoApplication(
        jobs,
        worker.startup,
        result_store,
        clock,
        prompts,
        principal_id=settings.principal_id,
        workspace_root=settings.workspace_root,
        output_root=settings.output_root,
        generation_timeout_seconds=settings.generation_timeout_seconds,
    )
    runtime_diagnostic = RunPodRuntimeDiagnostic.from_environment(environment)
    diagnostic_publisher = (
        RunPodRuntimeDiagnosticPublisher(result_store, runtime_diagnostic)
        if runtime_diagnostic is not None
        else None
    )
    return ServerlessDeployment(
        worker,
        RunPodHandler(application, prompts),
        diagnostic_publisher,
    )
