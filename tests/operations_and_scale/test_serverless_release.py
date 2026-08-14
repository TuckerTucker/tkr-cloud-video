"""Acceptance, Serverless parity, composition, and rollout tests."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from tests.conftest import CapturingEventSink
from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.core.logging import configure_logging
from tkr_cloud_video.jobs.contracts import GenerationRequest
from tkr_cloud_video.operations.metrics import InMemoryMetrics
from tkr_cloud_video.operations_and_scale.composition import (
    OperationsDependencies,
    compose_operations_and_scale,
)
from tkr_cloud_video.release.acceptance import (
    AcceptanceResult,
    AcceptanceScenario,
    ScenarioEvidence,
)
from tkr_cloud_video.release.rollout import (
    RolloutDecision,
    RolloutPolicy,
)
from tkr_cloud_video.release.runpod_handler import HandlerResponse, RunPodHandler


def request_payload() -> dict[str, object]:
    """Build the same canonical contract accepted by Interactive execution."""
    return {
        "schema_version": "1",
        "mode": "text-to-video",
        "workflow_id": "h3-t2v-1",
        "model_set_id": "h3-models-1",
        "prompt": "Synthetic prompt",
        "seed": 42,
    }


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
    response = await RunPodHandler(application).handle({"input": request_payload()})

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
    response = await RunPodHandler(application).handle(event)

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

    response = await RunPodHandler(application).handle({"input": request_payload()})

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

    handler = RunPodHandler(FailingApplication())
    response = await handler.handle(
        {
            "input": {
                "mode": "text-to-video",
                "workflow_id": "workflow-1",
                "model_set_id": "models-1",
                "prompt": "Synthetic prompt",
                "seed": 1,
            }
        }
    )

    assert response.ok is False
    assert response.error_code == "comfy_node_failed"
    assert response.error_context == {"resource_id": "104"}
