"""End-to-end generation through durable publish-last commit tests."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import FakeClock
from tests.durable_delivery.test_verified_result_commit import MemoryResultStore
from tests.job_execution.test_validated_job_intake import (
    ImageInspector,
    MemoryInputSource,
)
from tkr_cloud_video.application import CloudVideoApplication
from tkr_cloud_video.job_execution.composition import (
    JobDependencies,
    compose_job_execution,
)
from tkr_cloud_video.jobs.comfy_client import PromptState, PromptStatus
from tkr_cloud_video.jobs.contracts import TextToVideoRequest
from tkr_cloud_video.jobs.executor import Waiter
from tkr_cloud_video.jobs.media_validation import MediaInfo
from tkr_cloud_video.security.validation import Sha256Digest
from tkr_cloud_video.worker import HydratedWorkflow


@dataclass
class NoWait(Waiter):
    """Polling fake whose client completes on the first status read."""

    async def wait(self, _seconds: float) -> None:
        """Reject unexpected polling."""
        raise AssertionError("completed prompt must not poll")


@dataclass
class GeneratingComfyClient:
    """Create the exact file named by the approved output-prefix binding."""

    output_root: Path
    submissions: int = 0
    output: str | None = None

    async def submit(self, workflow: dict[str, Any], client_id: str) -> str:
        """Materialize synthetic media as the prompt-owned provider side effect."""
        self.submissions += 1
        prefix = workflow["20"]["inputs"]["filename_prefix"]
        assert isinstance(prefix, str) and client_id.startswith("attempt-")
        relative = f"{prefix}.mp4"
        path = self.output_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"verified-video")
        self.output = relative
        return "prompt-1"

    async def status(self, prompt_id: str) -> PromptStatus:
        """Return one successful prompt-owned output record."""
        assert prompt_id == "prompt-1" and self.output is not None
        return PromptStatus(PromptState.SUCCEEDED, (self.output,))

    async def cancel(self, _prompt_id: str) -> None:
        """Reject cancellation of the successful synthetic prompt."""
        raise AssertionError("successful prompt must not cancel")


class Inspector:
    """Valid deterministic ffprobe evidence fake."""

    def inspect(self, path: Path) -> MediaInfo:
        """Prove the exact synthetic media path."""
        assert path.read_bytes() == b"verified-video"
        return MediaInfo(2.0, 1280, 720, "h264", True)


@dataclass(frozen=True)
class Startup:
    """Approved hydrated workflow inventory fake."""

    value: HydratedWorkflow

    def workflow(self, workflow_id: str) -> HydratedWorkflow:
        """Return only the selected approved workflow."""
        assert workflow_id == self.value.name
        return self.value


def approved_workflow() -> HydratedWorkflow:
    """Build a workflow with prompt, seed, and server-owned output bindings."""
    content = json.dumps(
        {
            "10": {"class_type": "H3Sampler", "inputs": {"text": "", "seed": 0}},
            "20": {
                "class_type": "SaveVideo",
                "inputs": {"filename_prefix": ""},
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    import hashlib

    return HydratedWorkflow(
        "workflow-1",
        content,
        Sha256Digest(hashlib.sha256(content).hexdigest()),
        (
            ("prompt", "10", "text"),
            ("seed", "10", "seed"),
            ("output_prefix", "20", "filename_prefix"),
        ),
    )


@pytest.mark.asyncio
async def test_generation_commits_exact_artifacts_and_duplicate_converges(
    tmp_path: Path,
) -> None:
    """A repeated canonical request observes one durable publish-last result."""
    workspace_root, output_root = tmp_path / "workspaces", tmp_path / "outputs"
    workspace_root.mkdir()
    output_root.mkdir()
    client = GeneratingComfyClient(output_root)
    jobs = compose_job_execution(
        JobDependencies(
            FakeClock(current=datetime(2026, 8, 10, tzinfo=UTC), monotonic_value=0),
            NoWait(),
            client,
            MemoryInputSource(b"unused"),
            ImageInspector(),
            Inspector(),
            workspace_root,
            1024,
        )
    )
    store = MemoryResultStore()
    application = CloudVideoApplication(
        jobs,
        Startup(approved_workflow()),  # type: ignore[arg-type]
        store,
        FakeClock(current=datetime(2026, 8, 10, tzinfo=UTC)),
        principal_id="runpod-endpoint",
        workspace_root=workspace_root,
        output_root=output_root,
        generation_timeout_seconds=60,
    )
    request = TextToVideoRequest(
        mode="text-to-video",
        workflow_id="workflow-1",
        model_set_id="models-1",
        prompt="Synthetic prompt",
        seed=42,
    )

    first = await application.submit(request)
    duplicate = await application.submit(request)

    assert first == duplicate
    assert client.submissions == 1
    assert store.writes[-1] == first[1]
    assert first[1] is not None and store.objects[first[1]]
    assert not any(path.is_file() for path in output_root.rglob("*"))
    assert not any(path.is_dir() for path in workspace_root.glob("job-*"))


@dataclass
class DirectoryStatResultStore(MemoryResultStore):
    """Store reproducing the provider's absent-object read behavior.

    B2 has no materialized directories, so rclone answers a read for an absent
    object whose virtual parent exists with empty content and a success status
    rather than a not-found error. `head` already reports that case as absent.
    """

    async def get(self, key: str) -> bytes | None:
        """Return empty content for an absent object, as the provider does."""
        return self.objects.get(key, b"")


@pytest.mark.asyncio
async def test_absent_result_reading_as_empty_still_generates(tmp_path: Path) -> None:
    """An absent result that reads as empty must not be taken as committed."""
    workspace_root, output_root = tmp_path / "workspaces", tmp_path / "outputs"
    workspace_root.mkdir()
    output_root.mkdir()
    client = GeneratingComfyClient(output_root)
    jobs = compose_job_execution(
        JobDependencies(
            FakeClock(current=datetime(2026, 8, 10, tzinfo=UTC), monotonic_value=0),
            NoWait(),
            client,
            MemoryInputSource(b"unused"),
            ImageInspector(),
            Inspector(),
            workspace_root,
            1024,
        )
    )
    store = DirectoryStatResultStore()
    application = CloudVideoApplication(
        jobs,
        Startup(approved_workflow()),  # type: ignore[arg-type]
        store,
        FakeClock(current=datetime(2026, 8, 10, tzinfo=UTC)),
        principal_id="runpod-endpoint",
        workspace_root=workspace_root,
        output_root=output_root,
        generation_timeout_seconds=60,
    )
    request = TextToVideoRequest(
        mode="text-to-video",
        workflow_id="workflow-1",
        model_set_id="models-1",
        prompt="Synthetic prompt",
        seed=42,
    )

    _, reference = await application.submit(request)

    # The returned reference must name an object that was actually committed.
    assert client.submissions == 1
    assert reference is not None
    assert reference in store.objects


@pytest.mark.asyncio
async def test_generation_record_states_the_trained_envelope(tmp_path: Path) -> None:
    """A sub-envelope request is committed, and the record says it was one.

    The defect this closes was silence, not the request itself: defaults below the
    canvas and frame range the model was trained on validated cleanly and left no
    trace, so a disappointing render had nothing to point at.
    """
    workspace_root, output_root = tmp_path / "workspaces", tmp_path / "outputs"
    workspace_root.mkdir()
    output_root.mkdir()
    jobs = compose_job_execution(
        JobDependencies(
            FakeClock(current=datetime(2026, 8, 10, tzinfo=UTC), monotonic_value=0),
            NoWait(),
            GeneratingComfyClient(output_root),
            MemoryInputSource(b"unused"),
            ImageInspector(),
            Inspector(),
            workspace_root,
            1024,
        )
    )
    store = MemoryResultStore()
    application = CloudVideoApplication(
        jobs,
        Startup(approved_workflow()),  # type: ignore[arg-type]
        store,
        FakeClock(current=datetime(2026, 8, 10, tzinfo=UTC)),
        principal_id="runpod-endpoint",
        workspace_root=workspace_root,
        output_root=output_root,
        generation_timeout_seconds=60,
    )

    await application.submit(
        TextToVideoRequest(
            mode="text-to-video",
            workflow_id="workflow-1",
            model_set_id="models-1",
            prompt="Synthetic prompt",
            seed=42,
            width=864,
            height=480,
            frames=73,
        )
    )

    key = next(k for k in store.objects if k.endswith("generation.bin"))
    record = json.loads(store.objects[key])

    assert record["trained_envelope"] == {
        "trained_envelope_inside": False,
        "trained_envelope_short_edge_below": True,
        "trained_envelope_frames_below": True,
    }


@pytest.mark.asyncio
async def test_default_request_records_an_inside_envelope(tmp_path: Path) -> None:
    workspace_root, output_root = tmp_path / "workspaces", tmp_path / "outputs"
    workspace_root.mkdir()
    output_root.mkdir()
    jobs = compose_job_execution(
        JobDependencies(
            FakeClock(current=datetime(2026, 8, 10, tzinfo=UTC), monotonic_value=0),
            NoWait(),
            GeneratingComfyClient(output_root),
            MemoryInputSource(b"unused"),
            ImageInspector(),
            Inspector(),
            workspace_root,
            1024,
        )
    )
    store = MemoryResultStore()
    application = CloudVideoApplication(
        jobs,
        Startup(approved_workflow()),  # type: ignore[arg-type]
        store,
        FakeClock(current=datetime(2026, 8, 10, tzinfo=UTC)),
        principal_id="runpod-endpoint",
        workspace_root=workspace_root,
        output_root=output_root,
        generation_timeout_seconds=60,
    )

    await application.submit(
        TextToVideoRequest(
            mode="text-to-video",
            workflow_id="workflow-1",
            model_set_id="models-1",
            prompt="Synthetic prompt",
            seed=42,
        )
    )

    key = next(k for k in store.objects if k.endswith("generation.bin"))
    record = json.loads(store.objects[key])

    assert record["trained_envelope"]["trained_envelope_inside"] is True
    assert record["request"]["width"] == 1344
    assert record["request"]["frames"] == 124
