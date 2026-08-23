"""The console's use cases, from local refusal through to delivery."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.console.conftest import (
    CREDENTIALS,
    FakeObjectReader,
    FakeRunClient,
    SigningClock,
    settings,
)
from tests.console.test_signed_delivery import manifest
from tkr_cloud_video.console.presign import SigV4Presigner
from tkr_cloud_video.console.results import (
    AttemptResultRepository,
    JobRegistry,
    RegisteredJobAuthorizer,
    derive_identity,
)
from tkr_cloud_video.console.run_client import EndpointPausedError, RunStatus
from tkr_cloud_video.console.service import ConsoleService, compose_console
from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.delivery.access_policy import ResultAccessPolicy
from tkr_cloud_video.delivery.api import PrivateResultService
from tkr_cloud_video.delivery.signed_links import SignedLinkService
from tkr_cloud_video.prompt_authoring.composition import compose_prompt_authoring

ROOT = Path(__file__).resolve().parents[2]

REQUEST = {
    "schema_version": "1",
    "mode": "text-to-video",
    "workflow_id": "minimax-h3-t2v",
    "model_set_id": "minimax-h3-t2v-int8-20260809",
    "prompt": "A calm wide shot of a snow-covered pine forest at dawn.",
    "seed": 7,
}


def build(
    runs: FakeRunClient | None = None, reader: FakeObjectReader | None = None
) -> tuple[ConsoleService, FakeRunClient, FakeObjectReader, JobRegistry]:
    """Compose a console service over in-memory provider and storage fakes."""
    configuration = settings()
    run_client = runs or FakeRunClient()
    object_reader = reader or FakeObjectReader()
    registry = JobRegistry(configuration.principal_id)
    presigner = SigV4Presigner(
        CREDENTIALS,
        SigningClock(),
        bucket_name=configuration.bucket_name,
        endpoint=configuration.b2_s3_endpoint,
        region=configuration.b2_s3_region,
    )
    results = PrivateResultService(
        ResultAccessPolicy(RegisteredJobAuthorizer(registry)),
        AttemptResultRepository(registry, object_reader),
        SignedLinkService(
            presigner, SigningClock(), configuration.signed_link_ttl_seconds
        ),
    )
    service = ConsoleService(
        configuration,
        run_client,
        results,
        registry,
        compose_prompt_authoring(),
        SigningClock(),
    )
    return service, run_client, object_reader, registry


@pytest.mark.asyncio
async def test_a_defective_request_never_reaches_the_endpoint() -> None:
    """The whole point of the preflight: a defect costs no cold start."""
    service, runs, _, registry = build()

    payload = await service.generate({**REQUEST, "frames": 74})

    assert payload["ok"] is False
    assert runs.submitted == []
    assert list(registry) == []


@pytest.mark.asyncio
async def test_a_submitted_request_is_registered_under_its_derived_identity() -> None:
    """The console knows the result key before the worker has written it."""
    service, runs, _, _ = build()

    payload = await service.generate(REQUEST)

    assert payload["ok"] is True
    record: dict[str, Any] = payload["run"]  # type: ignore[assignment]
    job_id, _ = derive_identity("runpod-endpoint", record["request_hash"])
    assert record["job_id"] == job_id
    assert record["run_id"] == "run-1"
    assert runs.submitted[0]["input"]["width"] == 1344
    assert payload["wire_text"] == REQUEST["prompt"]


@pytest.mark.asyncio
async def test_a_paused_endpoint_is_reported_as_something_to_retry() -> None:
    """The page says wait rather than showing a failure."""
    runs = FakeRunClient(
        submit_error=EndpointPausedError(
            "endpoint_paused", "not resumed", retryable=True
        )
    )
    service, _, _, _ = build(runs=runs)

    payload = await service.generate(REQUEST)

    assert payload["ok"] is False
    assert payload["endpoint_paused"] is True
    assert payload["retryable"] is True


@pytest.mark.asyncio
async def test_a_committed_run_reports_completed_and_delivers_its_video() -> None:
    """State comes from the bucket; the link is reauthorized when it is issued."""
    service, runs, reader, registry = build()
    submitted = await service.generate(REQUEST)
    record = registry.by_run("run-1")
    assert record is not None
    reader.objects[record.commit_key] = manifest(
        record.job_id, record.attempt_id
    ).canonical_bytes()
    runs.statuses["run-1"] = RunStatus(
        run_id="run-1",
        status="COMPLETED",
        output={"ok": True, "job_id": record.job_id},
        delay_time_ms=7471,
        execution_time_ms=739856,
    )

    observed = await service.observe("run-1")
    link = await service.delivery_link(record.job_id)

    assert submitted["ok"] is True
    assert observed["result_state"] == "completed"
    assert observed["identity_matches"] is True
    assert link["ok"] is True
    assert "X-Amz-Expires=900" in str(link["url"])
    assert str(link["expires_at"]).startswith("2026-08-23T12:49:56")


@pytest.mark.asyncio
async def test_a_run_the_worker_refused_is_not_shown_as_a_success() -> None:
    """A COMPLETED run can carry a refusal, so the two are separated."""
    service, runs, _, registry = build()
    await service.generate(REQUEST)
    record = registry.by_run("run-1")
    assert record is not None
    runs.statuses["run-1"] = RunStatus(
        run_id="run-1",
        status="COMPLETED",
        output={
            "ok": False,
            "error_code": "prompt_vocabulary_violation",
            "defects": [
                {"field": "shots.0.camera.motion", "rule": "closed_vocabulary"}
            ],
        },
    )

    observed = await service.observe("run-1")

    handler: dict[str, Any] = observed["handler"]  # type: ignore[assignment]
    assert handler["ok"] is False
    assert handler["error_code"] == "prompt_vocabulary_violation"
    assert observed["result_state"] == "failed"


@pytest.mark.asyncio
async def test_a_failed_run_stops_reading_as_in_progress() -> None:
    """The bucket cannot tell a failed attempt from one still sampling."""
    service, runs, _, _ = build()
    await service.generate(REQUEST)
    runs.statuses["run-1"] = RunStatus(
        run_id="run-1", status="FAILED", error="worker died"
    )

    observed = await service.observe("run-1")

    assert observed["result_state"] == "failed"
    assert observed["provider_error"] == "worker died"


@pytest.mark.asyncio
async def test_a_principal_mismatch_is_named_rather_than_read_as_a_missing_result() -> (
    None
):
    """The one misconfiguration that is invisible until a generation succeeds."""
    service, runs, _, _ = build()
    await service.generate(REQUEST)
    runs.statuses["run-1"] = RunStatus(
        run_id="run-1",
        status="COMPLETED",
        output={"ok": True, "job_id": "job-" + "0" * 32},
    )

    observed = await service.observe("run-1")

    assert observed["identity_matches"] is False


@pytest.mark.asyncio
async def test_an_uncommitted_job_yields_no_delivery_link() -> None:
    """Nothing is signed for a job with no committed video."""
    service, _, _, registry = build()
    await service.generate(REQUEST)
    record = registry.by_run("run-1")
    assert record is not None

    payload = await service.delivery_link(record.job_id)

    assert payload["ok"] is False
    assert payload["error_code"] == "result_not_deliverable"


@pytest.mark.asyncio
async def test_a_job_this_console_never_submitted_is_not_deliverable() -> None:
    """An unauthorized job resolves the same as an absent one."""
    service, _, _, _ = build()

    payload = await service.delivery_link("job-" + "0" * 32)

    assert payload["ok"] is False
    assert (await service.result("job-" + "0" * 32))["state"] == "not-found"


@pytest.mark.asyncio
async def test_a_cancellation_is_passed_through() -> None:
    """The console can stop a run it started."""
    service, runs, _, _ = build()
    await service.generate(REQUEST)

    payload = await service.cancel("run-1")

    assert payload == {"ok": True, "run_id": "run-1", "status": "CANCELLED"}
    assert runs.cancelled == ["run-1"]


@pytest.mark.asyncio
async def test_an_unobserved_run_still_reports_the_provider_status() -> None:
    """A run id this session did not submit has no derived job to look up."""
    service, runs, _, _ = build()
    runs.statuses["run-elsewhere"] = RunStatus(
        run_id="run-elsewhere", status="IN_PROGRESS"
    )

    observed = await service.observe("run-elsewhere")

    assert observed["run"] is None
    assert "result_state" not in observed


@pytest.mark.asyncio
async def test_an_unreadable_bucket_degrades_an_observation_not_ends_it() -> None:
    """Where the run stands is still worth answering when delivery is unreadable.

    Returning an error document for the whole observation loses the provider
    status too, and the page renders such a document as a card that never
    changes. The half the console does know is kept, and the half it does not is
    named.
    """
    reader = FakeObjectReader(
        fail_with=AppError(
            "object_read_failed",
            "Reading a committed result marker failed.",
            context={"operation": "get_object"},
        )
    )
    service, runs, _, _ = build(reader=reader)
    await service.generate(REQUEST)
    runs.statuses["run-1"] = RunStatus(
        run_id="run-1", status="IN_PROGRESS", delay_time_ms=201_905
    )

    observed = await service.observe("run-1")

    assert observed["ok"] is True
    assert observed["status"] == "IN_PROGRESS"
    assert observed["delay_time_ms"] == 201_905
    assert observed["result_state"] is None
    assert observed["result_error"] == "object_read_failed"


@pytest.mark.asyncio
async def test_the_committed_metadata_is_served_without_a_delivery_link() -> None:
    """Lookup and delivery are separate, and only delivery signs anything."""
    service, _, reader, registry = build()
    await service.generate(REQUEST)
    record = registry.by_run("run-1")
    assert record is not None
    reader.objects[record.commit_key] = manifest(
        record.job_id, record.attempt_id
    ).canonical_bytes()

    payload = await service.result(record.job_id)

    assert payload["state"] == "completed"
    assert payload["video_bytes"] == 1024
    assert "url" not in payload


def test_the_scope_names_the_principal_a_mismatch_would_hide() -> None:
    """The console shows what it is pointed at, principal included."""
    service, _, _, _ = build()

    scope = service.scope()

    assert scope["endpoint_id"] == "176tpna3ogl94t"
    assert scope["principal_id"] == "runpod-endpoint"
    assert "runpod_api_key" not in scope


@pytest.mark.asyncio
async def test_the_history_lists_what_this_session_submitted() -> None:
    """The registry is the session's own record, newest first."""
    service, _, _, _ = build()
    await service.generate(REQUEST)

    history = service.history()

    assert history["ok"] is True
    assert len(history["runs"]) == 1  # type: ignore[arg-type]


def test_composition_yields_a_console_bound_to_loopback() -> None:
    """The composed console serves loopback and nothing else."""
    composed = compose_console(settings(), CREDENTIALS, SigningClock())

    assert composed.settings.bind_host == "127.0.0.1"
    assert composed.service.scope()["endpoint_id"] == "176tpna3ogl94t"


def test_the_console_never_composes_a_write_or_delete_capability() -> None:
    """Structural, not asserted: the console cannot upload, commit or erase.

    `durable_delivery/composition.py` makes the retention store optional so a
    process that must not delete is incapable of it rather than trusted. The
    console takes the same position one step further and composes none of the
    write side at all, which is a property of its source rather than of its
    configuration.
    """
    package = Path(compose_console.__module__.replace(".", "/")).parent
    sources = "\n".join(
        path.read_text() for path in (ROOT / "src" / package).glob("*.py")
    )

    for capability in (
        "VerifiedUploader",
        "ResultCommitter",
        "RetentionReconciler",
        "SubjectRequestService",
        "RetentionStore",
    ):
        assert capability not in sources


@pytest.mark.asyncio
async def test_every_provider_failure_becomes_an_error_document() -> None:
    """The page renders failures, so a traceback is the one thing it cannot show."""
    failure = AppError("provider_call_failed", "unreachable", retryable=True)
    service, runs, _, _ = build(runs=FakeRunClient(submit_error=failure))

    submitted = await service.generate(REQUEST)

    assert submitted["ok"] is False
    assert submitted["error_code"] == "provider_call_failed"
    assert submitted["endpoint_paused"] is False
    assert submitted["retryable"] is True

    runs.submit_error = None
    await service.generate(REQUEST)
    runs.fail_with = failure

    assert (await service.observe("run-1"))["error_code"] == "provider_call_failed"
    assert (await service.cancel("run-1"))["error_code"] == "provider_call_failed"


@pytest.mark.asyncio
async def test_an_unreadable_marker_is_reported_rather_than_raised() -> None:
    """A delivery route that raised would take the page down with it."""
    reader = FakeObjectReader()
    service, _, _, registry = build(reader=reader)
    await service.generate(REQUEST)
    record = next(iter(registry))
    reader.objects[record.commit_key] = b'{"schema_version": "9"}'

    payload = await service.delivery_link(record.job_id)

    assert payload["ok"] is False
    assert payload["error_code"] == "result_marker_invalid"


def test_the_service_reports_the_settings_it_was_composed_for() -> None:
    """The server reads the storage endpoint back off the service."""
    service, _, _, _ = build()

    assert service.settings.bucket_name == "tkr-video-ca"
