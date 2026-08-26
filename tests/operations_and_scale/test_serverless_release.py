"""Acceptance, Serverless parity, composition, and rollout tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast

import pytest

from tests.conftest import CapturingEventSink
from tests.job_execution.test_validated_job_intake import MODEL_SET_ID
from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.core.logging import configure_logging
from tkr_cloud_video.jobs.contracts import GenerationRequest
from tkr_cloud_video.operations.metrics import InMemoryMetrics
from tkr_cloud_video.operations_and_scale.composition import (
    OperationsDependencies,
    compose_operations_and_scale,
)
from tkr_cloud_video.prompt_authoring.composition import compose_prompt_authoring
from tkr_cloud_video.release.acceptance import (
    AcceptanceResult,
    AcceptanceScenario,
    ScenarioEvidence,
)
from tkr_cloud_video.release.rollout import (
    RolloutDecision,
    RolloutPolicy,
)
from tkr_cloud_video.release.runpod_handler import (
    HandlerResponse,
    JobApplication,
    RunPodHandler,
)
from tkr_cloud_video.release.serverless import ServerlessDeployment
from tkr_cloud_video.runtime.lifecycle import WorkerLifecycle, WorkerState


def request_payload() -> dict[str, object]:
    """Build the same canonical contract accepted by Interactive execution."""
    return {
        "schema_version": "1",
        "mode": "text-to-video",
        "workflow_id": "h3-t2v-1",
        "model_set_id": MODEL_SET_ID,
        "prompt": "Synthetic prompt",
        "seed": 42,
    }


def structured_payload(motion: str = "Static Shot") -> dict[str, object]:
    """Build the same contract carrying a structured prompt instead of freeform.

    Args:
        motion: The camera motion slot, so a caller can drive the accepted and
            the closed-vocabulary-defect branches from one payload.

    """
    return {
        "schema_version": "1",
        "mode": "text-to-video",
        "workflow_id": "h3-t2v-1",
        "model_set_id": MODEL_SET_ID,
        "seed": 42,
        "structured_prompt": {
            "mode": "T2VA",
            "duration_seconds": 3.04,
            "shots": [
                {
                    "number": 1,
                    "style": "watercolor",
                    "description": "A view across a mountain lake at sunrise.",
                    "camera": {"motion": motion, "amplitude": None, "speed": None},
                }
            ],
            "overall_soundscape": "Lapping lake waves under a light breeze.",
            "non_diegetic_music": "N/A",
        },
    }


def handler(application: JobApplication) -> RunPodHandler:
    """Build the handler over the real prompt boundary.

    The boundary is not faked: what these tests assert is that a prompt defect
    is reported without application work, and a fake that never rejects would
    make that assertion vacuous.
    """
    return RunPodHandler(application, compose_prompt_authoring())


@dataclass
class Application:
    """Shared application boundary fake for Serverless contract tests."""

    error: AppError | None = None
    requests: list[GenerationRequest] = field(default_factory=list)

    async def submit(self, request: GenerationRequest) -> tuple[str, str | None]:
        """Record the typed request or inject a safe application failure."""
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return "job-1", "outputs/job-1/attempt-1/result.json"


def warmup_deployment() -> tuple[ServerlessDeployment, list[dict[str, str]]]:
    """Build the narrow worker surface used by control-envelope tests."""
    lifecycle = WorkerLifecycle()
    starts: list[dict[str, str]] = []

    class Supervisor:
        async def start(self, environment: dict[str, str]) -> None:
            starts.append(environment)
            lifecycle.state = WorkerState.READY

    class RejectingHandler:
        def validate(self, event: object) -> HandlerResponse:
            return HandlerResponse(False, error_code="invalid_envelope")

    worker = SimpleNamespace(
        settings=SimpleNamespace(
            worker_id="worker-1",
            release_id="release-1",
            model_set_id="models-1",
        ),
        services=SimpleNamespace(
            lifecycle=lifecycle,
            supervisor=Supervisor(),
        ),
        comfy_environment={"SAFE_SETTING": "value"},
    )
    return ServerlessDeployment(worker, RejectingHandler()), starts  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_warmup_control_starts_worker_and_returns_safe_snapshot() -> None:
    """The exact control envelope readies a worker without generation intake."""
    deployment, starts = warmup_deployment()

    response = await deployment.handle(
        {"input": {"operation": "warmup", "schema_version": "1"}}
    )
    worker = cast(dict[str, Any], response["worker"])

    assert response["ok"] is True
    assert response["operation"] == "warmup"
    assert worker.keys() == {
        "worker_id",
        "release_id",
        "model_set_id",
        "state",
        "ready",
        "cold_start",
        "startup_seconds",
        "uptime_seconds",
        "observed_at",
    }
    assert worker | {
        "startup_seconds": None,
        "uptime_seconds": None,
        "observed_at": None,
    } == {
        "worker_id": "worker-1",
        "release_id": "release-1",
        "model_set_id": "models-1",
        "state": "ready",
        "ready": True,
        "cold_start": True,
        "startup_seconds": None,
        "uptime_seconds": None,
        "observed_at": None,
    }
    assert isinstance(worker["startup_seconds"], float)
    assert isinstance(worker["uptime_seconds"], float)
    assert isinstance(worker["observed_at"], str)
    assert starts == [{"SAFE_SETTING": "value"}]

    warm_response = await deployment.handle(
        {"input": {"schema_version": "1", "operation": "warmup"}}
    )
    warm_worker = cast(dict[str, Any], warm_response["worker"])
    assert warm_worker["cold_start"] is False
    assert starts == [{"SAFE_SETTING": "value"}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        {"input": {"operation": "warmup"}},
        {"input": {"operation": "warmup", "schema_version": "2"}},
        {
            "input": {
                "operation": "warmup",
                "schema_version": "1",
                "extra": True,
            }
        },
        {
            "input": {"operation": "warmup", "schema_version": "1"},
            "extra": True,
        },
    ],
)
async def test_near_warmup_envelopes_follow_normal_validation(
    event: object,
) -> None:
    """Malformed or extended controls cannot bypass the request contract."""
    deployment, starts = warmup_deployment()

    response = await deployment.handle(event)

    assert response["ok"] is False
    assert response["error_code"] == "invalid_envelope"
    assert starts == []


def evidence(
    *,
    missing: AcceptanceScenario | None = None,
    failing: AcceptanceScenario | None = None,
) -> tuple[ScenarioEvidence, ...]:
    """Build complete digest-pinned acceptance evidence with optional faults."""
    return tuple(
        ScenarioEvidence(scenario, scenario is not failing, "a" * 64)
        for scenario in AcceptanceScenario
        if scenario is not missing
    )


def test_acceptance_requires_every_unique_passing_scenario() -> None:
    """Any absent, duplicate, or failing criterion blocks promotion."""
    assert AcceptanceResult("release-1", evidence()).passed is True
    assert (
        AcceptanceResult(
            "release-1", evidence(missing=AcceptanceScenario.COLD_START)
        ).passed
        is False
    )
    assert (
        AcceptanceResult(
            "release-1", evidence(failing=AcceptanceScenario.STORAGE_FAILURE)
        ).passed
        is False
    )
    duplicate = (*evidence(), evidence()[0])
    assert AcceptanceResult("release-1", duplicate).passed is False


def test_acceptance_evidence_requires_digest_and_safe_release_id() -> None:
    """Mutable prose cannot stand in for evidence and IDs cannot contain paths."""
    with pytest.raises(AppError):
        ScenarioEvidence(AcceptanceScenario.COLD_START, True, "not-a-digest")
    with pytest.raises(AppError):
        AcceptanceResult("../release", ())


@pytest.mark.asyncio
async def test_serverless_handler_has_contract_parity_and_returns_references() -> None:
    """Serverless validates the shared request and never embeds media bytes."""
    application = Application()
    response = await handler(application).handle({"input": request_payload()})

    assert response == HandlerResponse(
        True,
        "job-1",
        "outputs/job-1/attempt-1/result.json",
    )
    assert len(application.requests) == 1
    assert application.requests[0].canonical_bytes()
    assert "mp4" not in repr(response).lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event", "error_code"),
    [
        ({}, "invalid_envelope"),
        ({"input": {"mode": "not-supported"}}, "invalid_request"),
    ],
)
async def test_serverless_handler_rejects_before_application(
    event: object, error_code: str
) -> None:
    """Malformed envelopes never reach downloads or GPU application work."""
    application = Application()
    response = await handler(application).handle(event)

    assert response == HandlerResponse(False, error_code=error_code)
    assert application.requests == []


@pytest.mark.asyncio
async def test_serverless_handler_preserves_typed_retry_classification() -> None:
    """Platform failures return safe retry metadata and remain uncommitted."""
    application = Application(
        AppError(
            "platform_unavailable",
            "Platform execution is unavailable.",
            retryable=True,
        )
    )

    response = await handler(application).handle({"input": request_payload()})

    assert response == HandlerResponse(
        False,
        error_code="platform_unavailable",
        retryable=True,
    )
    assert response.result_reference is None


@pytest.mark.parametrize(
    ("readiness", "error_rate", "accepted", "decision", "target"),
    [
        (1.0, 0.0, False, RolloutDecision.HOLD, "release-stable"),
        (0.90, 0.0, True, RolloutDecision.ROLLBACK, "release-stable"),
        (1.0, 0.06, True, RolloutDecision.ROLLBACK, "release-stable"),
        (0.99, 0.01, True, RolloutDecision.PROMOTE, "release-candidate"),
    ],
)
def test_rollout_selects_pinned_target_with_evidence(
    readiness: float,
    error_rate: float,
    accepted: bool,
    decision: RolloutDecision,
    target: str,
) -> None:
    """Threshold breaches deterministically target the last healthy release."""
    policy = RolloutPolicy(0.95, 0.05, "release-candidate", "release-stable")
    result = policy.evaluate(readiness, error_rate, accepted)

    assert result.decision is decision
    assert result.target_release_id == target
    assert result.reason


def test_rollout_rejects_unsafe_threshold_configuration() -> None:
    """Invalid ratios and an ambiguous rollback target fail at composition."""
    with pytest.raises(ValueError):
        RolloutPolicy(1.1, 0.1, "candidate", "stable")
    with pytest.raises(ValueError):
        RolloutPolicy(0.9, 0.1, "same", "same")


def test_operations_composition_wires_only_injected_dependencies() -> None:
    """The feature root owns no global clients, credentials, or provider state."""
    application = Application()
    metrics = InMemoryMetrics()
    logger = configure_logging(CapturingEventSink())
    services = compose_operations_and_scale(
        OperationsDependencies(
            logger,
            metrics,
            application,
            compose_prompt_authoring(),
            0.95,
            0.05,
            "release-candidate",
            "release-stable",
        )
    )

    assert services.metrics is metrics
    assert services.handler._application is application
    assert services.rollout.rollback_release_id == "release-stable"


@pytest.mark.asyncio
async def test_failure_response_names_what_failed() -> None:
    """A failed run reports the error's context, not the code alone.

    The code says a node failed; the context says which one. A caller holding
    only the response cannot read worker output to find out.
    """

    class FailingApplication:
        """Application whose submission fails at a named workflow node."""

        async def submit(self, request: object) -> tuple[str, str | None]:
            """Fail the way the executor does when ComfyUI reports a node error."""
            raise AppError(
                "comfy_node_failed",
                "ComfyUI reported a node execution failure.",
                context={"resource_id": "104"},
            )

    response = await handler(FailingApplication()).handle(
        {
            "input": {
                "mode": "text-to-video",
                "workflow_id": "workflow-1",
                "model_set_id": MODEL_SET_ID,
                "prompt": "Synthetic prompt",
                "seed": 1,
            }
        }
    )

    assert response.ok is False
    assert response.error_code == "comfy_node_failed"
    assert response.error_context == {"resource_id": "104"}


@pytest.mark.asyncio
async def test_structured_prompt_defect_is_reported_before_application_work() -> None:
    """A closed-vocabulary defect never reaches the application.

    The deployment starts ComfyUI between validation and application work, so
    a prompt rejected only at execution costs a full worker cold start to
    learn that a slot carried the template's angle brackets. Asserting on an
    untouched application is what pins the rejection ahead of that start.
    """
    application = Application()

    response = await handler(application).handle(
        {"input": structured_payload("<Static Shot>")}
    )

    assert response.ok is False
    assert response.error_code == "prompt_structurally_invalid"
    assert response.error_context == {
        "rule": "camera_motion_unknown",
        "field": "shots.0.camera.motion",
    }
    assert application.requests == []


@pytest.mark.asyncio
async def test_structured_prompt_rejection_carries_every_defect() -> None:
    """One rejection reports every broken rule, so one round trip fixes them all.

    `error_context` is a single flat mapping and can only name the first
    defect. A caller correcting a prompt one rule per submission pays a queue
    wait for each, which is the cost the defect list removes.
    """
    payload = structured_payload("<Static Shot>")
    prompt = payload["structured_prompt"]
    assert isinstance(prompt, dict)
    prompt["shots"][0]["style"] = "oil-painting"

    response = await handler(Application()).handle({"input": payload})

    assert response.defects is not None
    assert {defect["rule"] for defect in response.defects} == {
        "camera_motion_unknown",
        "style_unknown",
    }


@pytest.mark.asyncio
async def test_valid_structured_prompt_reaches_the_application_unchanged() -> None:
    """The preflight admits a renderable prompt without consuming it.

    The handler discards its rendered text, so the request the application
    receives must still carry the structured form for the binder to derive
    wire text from.
    """
    application = Application()

    response = await handler(application).handle({"input": structured_payload()})

    assert response.ok is True
    assert len(application.requests) == 1
    assert application.requests[0].structured_prompt is not None
    assert application.requests[0].prompt is None


@pytest.mark.asyncio
async def test_section_defect_is_reported_as_itself_before_application_work() -> None:
    """A section defect is refused ahead of the worker start like any other.

    Composition fails before the structural checks run, so this defect arrives
    as a different error type carrying no defect list. It still has to name its
    own rule rather than collapse into a generic invalid request.
    """
    payload = structured_payload()
    prompt = payload["structured_prompt"]
    assert isinstance(prompt, dict)
    del prompt["non_diegetic_music"]
    application = Application()

    response = await handler(application).handle({"input": payload})

    assert response.ok is False
    assert response.error_code == "section_set_mismatch_for_mode"
    assert response.defects is None
    assert application.requests == []
