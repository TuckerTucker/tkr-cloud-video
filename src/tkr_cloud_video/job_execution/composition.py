"""IoC composition for validated intake and deterministic execution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tkr_cloud_video.core.clock import Clock
from tkr_cloud_video.jobs.comfy_client import ComfyExecutionClient
from tkr_cloud_video.jobs.executor import PromptExecutor, Waiter
from tkr_cloud_video.jobs.identity import IdempotencyRegistry
from tkr_cloud_video.jobs.input_staging import (
    InputMediaInspector,
    InputSource,
    InputStager,
)
from tkr_cloud_video.jobs.media_validation import MediaInspector
from tkr_cloud_video.jobs.results import ResultValidator
from tkr_cloud_video.jobs.workflow_binder import WorkflowBinder
from tkr_cloud_video.jobs.workspace import WorkspaceManager


@dataclass(frozen=True, slots=True)
class JobDependencies:
    """All effects and policy configuration required by job services."""

    clock: Clock
    waiter: Waiter
    comfy_client: ComfyExecutionClient
    input_source: InputSource
    input_inspector: InputMediaInspector
    result_inspector: MediaInspector
    workspace_root: Path
    max_input_bytes: int


@dataclass(frozen=True, slots=True)
class JobServices:
    """Composed intake and deterministic execution services."""

    identities: IdempotencyRegistry
    workspaces: WorkspaceManager
    input_stager: InputStager
    workflow_binder: WorkflowBinder
    executor: PromptExecutor
    result_validator: ResultValidator


def compose_job_execution(dependencies: JobDependencies) -> JobServices:
    """Compose all job policies from explicit adapters."""
    return JobServices(
        IdempotencyRegistry(),
        WorkspaceManager(dependencies.workspace_root),
        InputStager(
            dependencies.input_source,
            dependencies.input_inspector,
            dependencies.max_input_bytes,
        ),
        WorkflowBinder(),
        PromptExecutor(
            dependencies.comfy_client, dependencies.clock, dependencies.waiter
        ),
        ResultValidator(dependencies.result_inspector),
    )
