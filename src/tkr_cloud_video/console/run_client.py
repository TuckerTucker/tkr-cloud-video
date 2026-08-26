"""Bounded client for the RunPod Serverless run routes.

Submission is asynchronous by construction, never `runsync`. A cold worker
hydrates the full model set before it accepts work, and the two runs recorded in
`docs/audits/2026-08-15-generation-latency.md` took 12 and 25 minutes end to
end. No synchronous HTTP call survives that, so the console submits, records the
run id, and polls.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Final, Protocol

from tkr_cloud_video.console.settings import ConsoleCredentials, ConsoleSettings
from tkr_cloud_video.core.errors import AppError

TERMINAL_STATUSES: Final[frozenset[str]] = frozenset(
    {"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"}
)
PAUSED_STATUS: Final[int] = 409
PAUSED_MARKER: Final[str] = "ENDPOINT_PAUSED"
MAX_RESPONSE_BYTES: Final[int] = 4_194_304


@dataclass(frozen=True, slots=True)
class RunStatus:
    """One observation of a submitted run.

    Args:
        run_id: The RunPod run identifier.
        status: The provider's status string, verbatim.
        output: The handler's own response, when the run has produced one.
        error: The provider's failure text, when it reported one.
        delay_time_ms: Queue time before a worker picked the run up.
        execution_time_ms: Time the worker spent on it.

    """

    run_id: str
    status: str
    output: dict[str, Any] | None = None
    error: str | None = None
    delay_time_ms: int | None = None
    execution_time_ms: int | None = None

    @property
    def terminal(self) -> bool:
        """Report whether the provider will not change this status again."""
        return self.status in TERMINAL_STATUSES


@dataclass(frozen=True, slots=True)
class EndpointHealth:
    """Bounded aggregate health exposed by the RunPod endpoint."""

    jobs_completed: int
    jobs_failed: int
    jobs_in_progress: int
    jobs_in_queue: int
    jobs_retried: int
    workers_idle: int
    workers_running: int


class EndpointPausedError(AppError):
    """The endpoint has not resumed, so it is refusing work for now.

    This is a state to wait out rather than a failure: raising a worker ceiling
    reaches the configuration plane before the run plane, so an endpoint that
    has just been un-paused still refuses submissions for a while.
    """


class RunClient(Protocol):
    """Submit, observe, and cancel port for one Serverless endpoint."""

    async def submit(self, payload: dict[str, Any]) -> str:
        """Submit one generation and return its run identifier."""
        ...

    async def status(self, run_id: str) -> RunStatus:
        """Observe one submitted run."""
        ...

    async def cancel(self, run_id: str) -> str:
        """Cancel one submitted run and return the status it reports after."""
        ...

    async def health(self) -> EndpointHealth:
        """Return aggregate endpoint queue and worker counts."""
        ...


class RunPodRunClient:
    """Submits, observes, and cancels runs on one Serverless endpoint."""

    def __init__(
        self, settings: ConsoleSettings, credentials: ConsoleCredentials
    ) -> None:
        """Initialize with the endpoint scope and its bearer credential."""
        self._base = f"{settings.runpod_api_base}/{settings.endpoint_id}"
        self._credentials = credentials
        self._timeout_seconds = settings.request_timeout_seconds

    def _call(self, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        """Perform one bounded blocking call to a run route."""
        request = urllib.request.Request(  # noqa: S310
            self._base + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {self._credentials.runpod_api_key}",
                "Content-Type": "application/json",
            },
            method="GET" if body is None else "POST",
        )
        try:
            # The URL is built from a validated https API base and an endpoint
            # identifier this process configured, never from a caller.
            with urllib.request.urlopen(  # noqa: S310
                request, timeout=self._timeout_seconds
            ) as response:
                payload = json.loads(response.read(MAX_RESPONSE_BYTES))
        except urllib.error.HTTPError as error:
            detail = error.read(2048).decode("utf-8", "replace")
            if error.code == PAUSED_STATUS and PAUSED_MARKER in detail:
                raise EndpointPausedError(
                    "endpoint_paused",
                    "The endpoint has not resumed and is refusing work.",
                    retryable=True,
                    context={"operation": "submit_run"},
                    cause=error,
                ) from error
            raise AppError(
                "provider_call_failed",
                "The RunPod run API rejected a call.",
                retryable=error.code >= 500,
                context={"operation": "call_run_api", "error_detail": str(error.code)},
                cause=error,
            ) from error
        except (OSError, json.JSONDecodeError) as error:
            raise AppError(
                "provider_call_failed",
                "The RunPod run API could not be reached.",
                retryable=True,
                context={"operation": "call_run_api"},
                cause=error,
            ) from error
        if not isinstance(payload, dict):
            raise AppError(
                "provider_response_invalid",
                "The RunPod run API returned a document that is not an object.",
                context={"operation": "call_run_api"},
            )
        return payload

    async def submit(self, payload: dict[str, Any]) -> str:
        """Submit one generation and return its run identifier.

        Args:
            payload: The full ``{"input": {...}}`` envelope.

        Returns:
            The RunPod run identifier.

        Raises:
            EndpointPausedError: The endpoint has not resumed yet.
            AppError: The provider refused the submission or answered without
                a run identifier.

        """
        response = await asyncio.to_thread(self._call, "/run", payload)
        run_id = response.get("id")
        if not isinstance(run_id, str) or not run_id:
            raise AppError(
                "provider_response_invalid",
                "The RunPod run API accepted a submission without naming a run.",
                context={"operation": "submit_run"},
            )
        return run_id

    async def status(self, run_id: str) -> RunStatus:
        """Observe one submitted run."""
        response = await asyncio.to_thread(self._call, f"/status/{run_id}", None)
        status = response.get("status")
        if not isinstance(status, str):
            raise AppError(
                "provider_response_invalid",
                "The RunPod run API answered a status query without a status.",
                context={"operation": "observe_run"},
            )
        output = response.get("output")
        error = response.get("error")
        return RunStatus(
            run_id=run_id,
            status=status,
            output=output if isinstance(output, dict) else None,
            error=error if isinstance(error, str) else None,
            delay_time_ms=_optional_int(response.get("delayTime")),
            execution_time_ms=_optional_int(response.get("executionTime")),
        )

    async def cancel(self, run_id: str) -> str:
        """Cancel one submitted run and return the status it reports after."""
        response = await asyncio.to_thread(self._call, f"/cancel/{run_id}", {})
        status = response.get("status")
        return status if isinstance(status, str) else "CANCELLED"

    async def health(self) -> EndpointHealth:
        """Return validated aggregate queue and worker counts."""
        response = await asyncio.to_thread(self._call, "/health", None)
        jobs = response.get("jobs")
        workers = response.get("workers")
        if not isinstance(jobs, dict) or not isinstance(workers, dict):
            raise AppError(
                "provider_response_invalid",
                "The RunPod health API returned an incomplete document.",
                context={"operation": "observe_endpoint_health"},
            )
        try:
            return EndpointHealth(
                jobs_completed=_count(jobs, "completed"),
                jobs_failed=_count(jobs, "failed"),
                jobs_in_progress=_count(jobs, "inProgress"),
                jobs_in_queue=_count(jobs, "inQueue"),
                jobs_retried=_count(jobs, "retried"),
                workers_idle=_count(workers, "idle"),
                workers_running=_count(workers, "running"),
            )
        except ValueError as error:
            raise AppError(
                "provider_response_invalid",
                "The RunPod health API returned an invalid count.",
                context={"operation": "observe_endpoint_health"},
                cause=error,
            ) from error


def _optional_int(value: object) -> int | None:
    """Return an integer timing, or None when absent or unusable.

    Booleans are excluded explicitly: they are integers in Python, and a
    provider that answered ``true`` would otherwise be read as one millisecond.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _count(group: dict[str, Any], name: str) -> int:
    """Read one required non-negative provider count."""
    value = group.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(name)
    return value
