"""End-to-end generation, exact output validation, and durable commit use case."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from tkr_cloud_video.core.clock import Clock
from tkr_cloud_video.delivery.commit import ResultCommitter
from tkr_cloud_video.delivery.contracts import ResultManifest, ResultRole
from tkr_cloud_video.delivery.uploader import (
    LocalArtifact,
    ResultStore,
    VerifiedUploader,
)
from tkr_cloud_video.job_execution.composition import JobServices
from tkr_cloud_video.jobs.contracts import (
    GenerationRequest,
    ImageToVideoRequest,
    ReferenceToVideoRequest,
)
from tkr_cloud_video.jobs.identity import JobIdentity, JobState
from tkr_cloud_video.jobs.workflow_binder import ParameterBinding
from tkr_cloud_video.security.validation import Sha256Digest
from tkr_cloud_video.worker import WorkerStartupSteps


class CloudVideoApplication:
    """Own one deterministic request through verified durable publication."""

    def __init__(
        self,
        jobs: JobServices,
        startup: WorkerStartupSteps,
        result_store: ResultStore,
        clock: Clock,
        *,
        principal_id: str,
        workspace_root: Path,
        output_root: Path,
        generation_timeout_seconds: float,
    ) -> None:
        """Initialize all provider-neutral policies and exact local roots."""
        if generation_timeout_seconds <= 0:
            raise ValueError("generation timeout must be positive")
        self._jobs = jobs
        self._startup = startup
        self._store = result_store
        self._uploader = VerifiedUploader(result_store)
        self._committer = ResultCommitter(result_store)
        self._clock = clock
        self._principal_id = principal_id
        self._workspace_root = workspace_root.resolve()
        self._output_root = output_root.resolve()
        self._generation_timeout = generation_timeout_seconds
        self._locks: dict[str, asyncio.Lock] = {}

    async def submit(self, request: GenerationRequest) -> tuple[str, str | None]:
        """Execute or converge a duplicate on its deterministic committed result."""
        request_hash = request.request_hash()
        identity = _job_identity(self._principal_id, request_hash)
        commit_key = f"outputs/{identity.job_id}/{identity.attempt_id}/result.json"
        lock = self._locks.setdefault(request_hash, asyncio.Lock())
        async with lock:
            if await self._store.get(commit_key) is not None:
                return identity.job_id, commit_key
            return await self._execute(request, identity, request_hash, commit_key)

    async def _execute(
        self,
        request: GenerationRequest,
        identity: JobIdentity,
        request_hash: str,
        commit_key: str,
    ) -> tuple[str, str]:
        workflow = self._startup.workflow(request.workflow_id)
        workspace = self._jobs.workspaces.allocate(identity)
        media_path: Path | None = None
        try:
            runtime_values = await self._stage_inputs(request, workspace.inputs)
            runtime_values["output_prefix"] = (
                f"{identity.job_id}/{identity.attempt_id}/video"
            )
            bindings = tuple(
                ParameterBinding(source, node_id, input_name)
                for source, node_id, input_name in workflow.bindings
            )
            bound = self._jobs.workflow_binder.bind(
                workflow.content,
                workflow.sha256,
                bindings,
                request,
                runtime_values,
            )
            status = await self._jobs.executor.execute(
                bound, identity.attempt_id, self._generation_timeout
            )
            result = self._jobs.result_validator.validate(
                self._output_root,
                status.outputs,
                job_id=identity.job_id,
                attempt_id=identity.attempt_id,
                workflow_digest=workflow.sha256,
                model_set_id=request.model_set_id,
                request_hash=Sha256Digest(request_hash),
            )
            media_path = result.media_path
            workflow_artifact = _write_canonical(
                workspace.metadata / "workflow.json", bound
            )
            generation_artifact = _write_canonical(
                workspace.metadata / "generation.json",
                {
                    "schema_version": "1",
                    "job_id": identity.job_id,
                    "attempt_id": identity.attempt_id,
                    "request": request.model_dump(mode="json"),
                    "request_hash": request_hash,
                    "model_set_id": request.model_set_id,
                    "workflow_digest": str(workflow.sha256),
                    "media": asdict(result.media),
                },
            )
            local = (
                LocalArtifact(
                    ResultRole.VIDEO,
                    result.media_path,
                    str(result.sha256),
                    result.size_bytes,
                ),
                _local_artifact(ResultRole.WORKFLOW, workflow_artifact),
                _local_artifact(ResultRole.GENERATION, generation_artifact),
            )
            evidence = await self._uploader.upload(
                identity.job_id, identity.attempt_id, local
            )
            manifest = ResultManifest(
                job_id=identity.job_id,
                attempt_id=identity.attempt_id,
                workflow_digest=str(workflow.sha256),
                model_set_id=request.model_set_id,
                request_hash=request_hash,
                committed_at=self._clock.now(),
                artifacts=evidence,
            )
            await self._committer.commit(manifest)
            return identity.job_id, commit_key
        finally:
            self._jobs.workspaces.cleanup(workspace)
            if media_path is not None:
                media_path.unlink(missing_ok=True)

    async def _stage_inputs(
        self, request: GenerationRequest, input_root: Path
    ) -> dict[str, str]:
        references = (
            (request.image,)
            if isinstance(request, ImageToVideoRequest)
            else request.references
            if isinstance(request, ReferenceToVideoRequest)
            else ()
        )
        values: dict[str, str] = {}
        for index, reference in enumerate(references):
            staged = await self._jobs.input_stager.stage(
                self._principal_id, reference, input_root, index
            )
            relative = staged.path.resolve().relative_to(self._workspace_root)
            values[f"input_{index}"] = relative.as_posix()
        return values


def _job_identity(principal_id: str, request_hash: str) -> JobIdentity:
    digest = hashlib.sha256(f"{principal_id}:{request_hash}".encode()).hexdigest()
    return JobIdentity(
        f"job-{digest[:32]}",
        f"attempt-{digest[32:]}",
        principal_id,
        request_hash,
        request_hash,
        JobState.ACCEPTED,
    )


def _write_canonical(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")))
    return path


def _local_artifact(role: ResultRole, path: Path) -> LocalArtifact:
    content = path.read_bytes()
    return LocalArtifact(
        role,
        path,
        hashlib.sha256(content).hexdigest(),
        len(content),
    )
