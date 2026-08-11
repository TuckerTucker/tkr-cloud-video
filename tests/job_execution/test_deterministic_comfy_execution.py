"""Pinned workflow, bounded prompt, and exact result validation tests."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tests.job_execution.test_validated_job_intake import (
    ImageInspector,
    MemoryInputSource,
)
from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.job_execution.composition import (
    JobDependencies,
    compose_job_execution,
)
from tkr_cloud_video.jobs.comfy_client import (
    ComfyExecutionClient,
    PromptState,
    PromptStatus,
)
from tkr_cloud_video.jobs.contracts import TextToVideoRequest
from tkr_cloud_video.jobs.executor import JobExecutionError, PromptExecutor, Waiter
from tkr_cloud_video.jobs.media_validation import MediaInfo, MediaInspector
from tkr_cloud_video.jobs.results import ResultValidator
from tkr_cloud_video.jobs.workflow_binder import (
    ParameterBinding,
    WorkflowBinder,
    WorkflowBindingError,
)
from tkr_cloud_video.security.validation import Sha256Digest


def request() -> TextToVideoRequest:
    """Build one normalized request."""
    return TextToVideoRequest(
        mode="text-to-video",
        workflow_id="h3-t2v-1",
        model_set_id="h3-models-1",
        prompt="Synthetic prompt",
        seed=42,
    )


def workflow() -> bytes:
    """Return canonical API workflow bytes."""
    return json.dumps(
        {"10": {"class_type": "H3Sampler", "inputs": {"seed": 0, "text": ""}}},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def test_binder_changes_only_allowlisted_inputs_after_digest_check() -> None:
    """Binding preserves class and undeclared graph data."""
    content = workflow()
    bound = WorkflowBinder().bind(
        content,
        Sha256Digest(hashlib.sha256(content).hexdigest()),
        (
            ParameterBinding("seed", "10", "seed"),
            ParameterBinding("prompt", "10", "text"),
        ),
        request(),
    )
    assert bound["10"]["inputs"] == {"seed": 42, "text": "Synthetic prompt"}
    assert bound["10"]["class_type"] == "H3Sampler"


def test_binder_rejects_drift_or_undeclared_node() -> None:
    """Workflow substitution and missing mapping targets fail before submit."""
    content = workflow()
    with pytest.raises(WorkflowBindingError):
        WorkflowBinder().bind(content, Sha256Digest("a" * 64), (), request())
    with pytest.raises(WorkflowBindingError):
        WorkflowBinder().bind(
            content,
            Sha256Digest(hashlib.sha256(content).hexdigest()),
            (ParameterBinding("seed", "missing", "seed"),),
            request(),
        )


@dataclass
class FakeClock:
    """Advancing monotonic clock."""

    value: float = 0

    def now(self) -> Any:
        """Wall time is unused by prompt execution."""
        raise AssertionError("unused")

    def monotonic(self) -> float:
        """Return current synthetic monotonic time."""
        return self.value


@dataclass
class AdvancingWaiter(Waiter):
    """Advances fake time instead of sleeping."""

    clock: FakeClock

    async def wait(self, seconds: float) -> None:
        """Advance by the requested delay."""
        self.clock.value += seconds


@dataclass
class FakeClient(ComfyExecutionClient):
    """Sequence-driven prompt client."""

    statuses: list[PromptStatus]
    cancelled: list[str] = field(default_factory=list)

    async def submit(self, workflow: dict[str, Any], client_id: str) -> str:
        """Return a stable prompt identity."""
        assert workflow and client_id
        return "prompt-1"

    async def status(self, prompt_id: str) -> PromptStatus:
        """Return the next configured status."""
        assert prompt_id == "prompt-1"
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

    async def cancel(self, prompt_id: str) -> None:
        """Record exact prompt cancellation."""
        self.cancelled.append(prompt_id)


@pytest.mark.asyncio
async def test_executor_correlates_until_success() -> None:
    """Running state converges on the exact prompt-owned output record."""
    clock = FakeClock()
    client = FakeClient(
        [
            PromptStatus(PromptState.RUNNING),
            PromptStatus(PromptState.SUCCEEDED, ("video.mp4",)),
        ]
    )
    result = await PromptExecutor(client, clock, AdvancingWaiter(clock)).execute(
        {"10": {}}, "attempt-1", 2
    )
    assert result.outputs == ("video.mp4",)
    assert client.cancelled == []


@pytest.mark.asyncio
async def test_executor_cancels_timeout_and_classifies_node_failure() -> None:
    """Timeout is retryable; node errors are safe and terminal."""
    clock = FakeClock()
    client = FakeClient([PromptStatus(PromptState.RUNNING)])
    with pytest.raises(JobExecutionError) as timeout:
        await PromptExecutor(client, clock, AdvancingWaiter(clock)).execute(
            {"10": {}}, "attempt-1", 0.5
        )
    assert timeout.value.retryable and client.cancelled == ["prompt-1"]

    failure = FakeClient([PromptStatus(PromptState.FAILED, error_node="node-10")])
    with pytest.raises(JobExecutionError) as captured:
        await PromptExecutor(
            failure, FakeClock(), AdvancingWaiter(FakeClock())
        ).execute({"10": {}}, "attempt-1", 1)
    assert captured.value.context["resource_id"] == "node-10"


class VideoInspector(MediaInspector):
    """Valid video inspection fake."""

    def inspect(self, path: Path) -> MediaInfo:
        """Return stable ffprobe-like evidence."""
        assert path.suffix == ".mp4"
        return MediaInfo(3.0, 1280, 720, "h264", True)


def test_result_validator_uses_exact_prompt_output_without_scan(tmp_path: Path) -> None:
    """Only the named contained output becomes upload ready."""
    expected = tmp_path / "expected.mp4"
    expected.write_bytes(b"video")
    (tmp_path / "unrelated.mp4").write_bytes(b"unrelated")
    result = ResultValidator(VideoInspector()).validate(
        tmp_path,
        ("expected.mp4",),
        job_id="job-1",
        attempt_id="attempt-1",
        workflow_digest=Sha256Digest("a" * 64),
        model_set_id="models-1",
        request_hash=Sha256Digest("b" * 64),
    )
    assert result.media_path == expected
    assert result.size_bytes == 5
    assert result.sha256 == Sha256Digest(hashlib.sha256(b"video").hexdigest())


@pytest.mark.parametrize("outputs", [(), ("one.mp4", "two.mp4"), ("../escape.mp4",)])
def test_result_validator_rejects_missing_ambiguous_or_escaping_paths(
    tmp_path: Path, outputs: tuple[str, ...]
) -> None:
    """Invalid prompt records never yield an upload-ready result."""
    with pytest.raises(AppError):
        ResultValidator(VideoInspector()).validate(
            tmp_path,
            outputs,
            job_id="job-1",
            attempt_id="attempt-1",
            workflow_digest=Sha256Digest("a" * 64),
            model_set_id="models-1",
            request_hash=Sha256Digest("b" * 64),
        )


def test_job_composition_wires_all_injected_adapters(tmp_path: Path) -> None:
    """The job feature root constructs every policy without ambient clients."""
    source = MemoryInputSource(b"image")
    client = FakeClient([PromptStatus(PromptState.SUCCEEDED, ("video.mp4",))])
    clock = FakeClock()
    services = compose_job_execution(
        JobDependencies(
            clock,
            AdvancingWaiter(clock),
            client,
            source,
            ImageInspector(),
            VideoInspector(),
            tmp_path,
            1024,
        )
    )

    assert services.input_stager._source is source
    assert services.executor._client is client
    assert services.workspaces is not None
    assert services.workflow_binder is not None
    assert services.result_validator is not None
