"""The console's use cases, composed from least-privilege parts.

Only three of the delivery services are composed here: the access policy, the
signing service, and the private result service. The uploader, the committer,
the reconciler, and the subject-request service are all absent, so this process
is structurally incapable of writing to or deleting from the delivery bucket
rather than merely trusted not to. That is the same reasoning
`durable_delivery/composition.py` gives for making its retention store optional.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tkr_cloud_video.console.intake import Accepted, Rejection, preflight
from tkr_cloud_video.console.objects import PresignedObjectReader
from tkr_cloud_video.console.presign import SigV4Presigner
from tkr_cloud_video.console.results import (
    AttemptResultRepository,
    JobRecord,
    JobRegistry,
    RegisteredJobAuthorizer,
)
from tkr_cloud_video.console.run_client import (
    EndpointPausedError,
    RunClient,
    RunPodRunClient,
    RunStatus,
)
from tkr_cloud_video.console.settings import ConsoleCredentials, ConsoleSettings
from tkr_cloud_video.core.clock import Clock, SystemClock
from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.delivery.access_policy import ResultAccessPolicy, ResultState
from tkr_cloud_video.delivery.api import PrivateResultService
from tkr_cloud_video.delivery.contracts import ResultRole
from tkr_cloud_video.delivery.signed_links import SignedLinkService
from tkr_cloud_video.prompt_authoring.composition import (
    PromptServices,
    compose_prompt_authoring,
)


class ConsoleService:
    """Submits generations, observes them, and delivers what they committed."""

    def __init__(
        self,
        settings: ConsoleSettings,
        runs: RunClient,
        results: PrivateResultService,
        registry: JobRegistry,
        prompts: PromptServices,
        clock: Clock,
    ) -> None:
        """Initialize from already-composed collaborators."""
        self._settings = settings
        self._runs = runs
        self._results = results
        self._registry = registry
        self._prompts = prompts
        self._clock = clock

    @property
    def settings(self) -> ConsoleSettings:
        """Return the settings this console was composed for."""
        return self._settings

    def preflight(self, payload: object) -> dict[str, object]:
        """Validate a request without submitting it."""
        return preflight(payload, self._prompts).as_payload()

    async def generate(self, payload: object) -> dict[str, object]:
        """Validate a request and, if it holds, submit it.

        A rejected request never reaches the endpoint, so a defect costs a round
        trip to this process rather than a worker cold start.

        Args:
            payload: The untrusted ``input`` document.

        Returns:
            The rejection document, or the accepted run with the job identity
            the console derived for it.

        """
        checked = preflight(payload, self._prompts)
        if isinstance(checked, Rejection):
            return checked.as_payload()
        try:
            envelope = {"input": checked.request.model_dump(mode="json")}
            run_id = await self._runs.submit(envelope)
        except EndpointPausedError as error:
            return _error_payload(error, endpoint_paused=True)
        except AppError as error:
            return _error_payload(error)
        record = self._registry.register(
            request_hash=checked.request_hash,
            run_id=run_id,
            submitted_at=self._clock.now(),
            model_set_id=checked.request.model_set_id,
            below_trained_envelope=checked.below_trained_envelope,
        )
        return {"ok": True, "run": _record_payload(record), **_accepted_extras(checked)}

    async def observe(self, run_id: str) -> dict[str, object]:
        """Report where one submitted run stands, and what it committed.

        The provider answers whether the run is queued, executing, or finished.
        The delivery bucket answers whether anything was committed. Neither
        alone is the whole state: a run can report ``COMPLETED`` for a handler
        response that refused the request, and a committed result is only
        readable once its marker exists.
        """
        record = self._registry.by_run(run_id)
        try:
            status = await self._runs.status(run_id)
        except AppError as error:
            return _error_payload(error)
        payload: dict[str, object] = {
            "ok": True,
            "run_id": run_id,
            "status": status.status,
            "terminal": status.terminal,
            "delay_time_ms": status.delay_time_ms,
            "execution_time_ms": status.execution_time_ms,
            "provider_error": status.error,
            "run": None if record is None else _record_payload(record),
        }
        payload["handler"] = _handler_payload(status)
        if record is None:
            return payload
        payload["identity_matches"] = _identity_matches(status, record)
        try:
            payload["result_state"] = await self._result_state(record, status)
        except AppError as error:
            # The provider already answered where the run stands, and that is
            # the more useful half of an observation. A delivery bucket this
            # console could not read degrades the answer rather than replacing
            # it with an error document the page would render as nothing.
            payload["result_state"] = None
            payload["result_error"] = error.code
        return payload

    async def cancel(self, run_id: str) -> dict[str, object]:
        """Cancel one submitted run."""
        try:
            status = await self._runs.cancel(run_id)
        except AppError as error:
            return _error_payload(error)
        return {"ok": True, "run_id": run_id, "status": status}

    async def delivery_link(
        self, job_id: str, ttl_seconds: int | None = None
    ) -> dict[str, object]:
        """Issue a short-lived playback link for a committed video.

        The link is reauthorized at issue rather than inherited from the lookup
        that preceded it, and it is returned to the caller only. It is never
        logged, never placed in an error context, and never persisted.
        """
        ttl = ttl_seconds or self._settings.signed_link_ttl_seconds
        try:
            link = await self._results.download(
                self._registry.principal_id, job_id, ttl
            )
        except AppError as error:
            return _error_payload(error)
        if link is None:
            return {
                "ok": False,
                "error_code": "result_not_deliverable",
                "message": "No committed video is available for this job.",
                "defects": [],
                "error_context": {"job_id": job_id},
            }
        return {
            "ok": True,
            "job_id": job_id,
            "url": link.url,
            "expires_at": link.expires_at.isoformat(),
        }

    async def result(self, job_id: str) -> dict[str, object]:
        """Return the committed metadata for a job, without a delivery link."""
        response = await self._results.lookup(self._registry.principal_id, job_id)
        manifest = response.manifest
        return {
            "ok": True,
            "job_id": job_id,
            "state": str(response.state),
            "manifest": None if manifest is None else manifest.model_dump(mode="json"),
            "video_bytes": None
            if manifest is None
            else next(
                artifact.size_bytes
                for artifact in manifest.artifacts
                if artifact.role is ResultRole.VIDEO
            ),
        }

    def scope(self) -> dict[str, object]:
        """Return what this console is pointed at.

        The principal is shown because it is the one setting whose mismatch is
        invisible until a generation succeeds and then reads as never
        committed: job identity is derived from it, so a console configured
        with a different principal than the endpoint computes the wrong keys.
        """
        return {
            "ok": True,
            "endpoint_id": self._settings.endpoint_id,
            "principal_id": self._settings.principal_id,
            "bucket_name": self._settings.bucket_name,
            "region": self._settings.b2_s3_region,
            "signed_link_ttl_seconds": self._settings.signed_link_ttl_seconds,
        }

    def history(self) -> dict[str, object]:
        """Return what this console session has submitted, newest first."""
        return {
            "ok": True,
            "runs": [_record_payload(record) for record in self._registry],
        }

    async def _result_state(self, record: JobRecord, status: RunStatus) -> str:
        """Resolve the committed state, overlaying a failed run.

        The bucket cannot tell a failed attempt from one still sampling, so a
        run that reached a terminal non-success status is reported as failed
        here rather than left reading as in progress forever.
        """
        state = await self._results.lookup(self._registry.principal_id, record.job_id)
        if state.state is ResultState.COMPLETED:
            return str(ResultState.COMPLETED)
        if status.terminal and status.status != "COMPLETED":
            return str(ResultState.FAILED)
        if status.terminal and not _handler_accepted(status):
            return str(ResultState.FAILED)
        return str(state.state)


def _handler_accepted(status: RunStatus) -> bool:
    """Report whether the handler's own response accepted the request."""
    return bool(status.output and status.output.get("ok") is True)


def _handler_payload(status: RunStatus) -> dict[str, object] | None:
    """Return the handler's response, which is a rejection when ``ok`` is false.

    A run can be ``COMPLETED`` for a request the handler refused: the provider
    reports that the worker answered, not that it generated anything. The two
    are separated here so the page never shows a refusal as a success.
    """
    if status.output is None:
        return None
    return {
        "ok": status.output.get("ok") is True,
        "job_id": status.output.get("job_id"),
        "result_reference": status.output.get("result_reference"),
        "error_code": status.output.get("error_code"),
        "retryable": status.output.get("retryable"),
        "error_context": status.output.get("error_context"),
        "defects": status.output.get("defects") or [],
    }


def _identity_matches(status: RunStatus, record: JobRecord) -> bool | None:
    """Compare the worker's job id with the one the console derived.

    A mismatch has exactly one cause worth naming: the console's principal is
    not the endpoint's. Because a job's identity is a digest of the principal
    and the request, a console configured with the wrong one computes keys for
    objects the worker never wrote, and every generation reads as uncommitted.
    """
    reported = None if status.output is None else status.output.get("job_id")
    if not isinstance(reported, str):
        return None
    return reported == record.job_id


def _record_payload(record: JobRecord) -> dict[str, object]:
    """Return one submitted generation as the page sees it."""
    return {
        "run_id": record.run_id,
        "job_id": record.job_id,
        "request_hash": record.request_hash,
        "submitted_at": record.submitted_at.isoformat(),
        "model_set_id": record.model_set_id,
        "below_trained_envelope": record.below_trained_envelope,
    }


def _accepted_extras(accepted: Accepted) -> dict[str, object]:
    """Return the parts of a preflight worth repeating beside a submission."""
    payload = accepted.as_payload()
    return {
        "request": payload["request"],
        "wire_text": payload["wire_text"],
        "trained_envelope": payload["trained_envelope"],
    }


def _error_payload(error: AppError, *, endpoint_paused: bool = False) -> dict[str, Any]:
    """Return one application error as the page's error document."""
    return {
        "ok": False,
        "error_code": error.code,
        "message": str(error),
        "retryable": error.retryable,
        "endpoint_paused": endpoint_paused,
        "error_context": dict(error.context) or None,
        "defects": [],
    }


@dataclass(frozen=True, slots=True)
class ComposedConsole:
    """The composed console and the collaborators a caller may still need."""

    service: ConsoleService
    settings: ConsoleSettings


def compose_console(
    settings: ConsoleSettings,
    credentials: ConsoleCredentials,
    clock: Clock | None = None,
) -> ComposedConsole:
    """Compose the console from its settings and its three secrets.

    Args:
        settings: Validated non-secret configuration.
        credentials: The RunPod bearer token and the delivery-read pair.
        clock: Injected time source, defaulting to the system clock.

    Returns:
        The composed console.

    """
    resolved_clock = clock or SystemClock()
    registry = JobRegistry(settings.principal_id)
    presigner = SigV4Presigner(
        credentials,
        resolved_clock,
        bucket_name=settings.bucket_name,
        endpoint=settings.b2_s3_endpoint,
        region=settings.b2_s3_region,
    )
    reader = PresignedObjectReader(
        presigner,
        ttl_seconds=settings.read_link_ttl_seconds,
        timeout_seconds=settings.request_timeout_seconds,
    )
    results = PrivateResultService(
        ResultAccessPolicy(RegisteredJobAuthorizer(registry)),
        AttemptResultRepository(registry, reader),
        SignedLinkService(presigner, resolved_clock, settings.signed_link_ttl_seconds),
    )
    service = ConsoleService(
        settings,
        RunPodRunClient(settings, credentials),
        results,
        registry,
        compose_prompt_authoring(),
        resolved_clock,
    )
    return ComposedConsole(service=service, settings=settings)
