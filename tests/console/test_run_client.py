"""The console's client for the RunPod run routes."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from io import BytesIO
from typing import Any

import pytest

from tests.console.conftest import CREDENTIALS, settings
from tkr_cloud_video.console.run_client import (
    EndpointHealth,
    EndpointPausedError,
    RunPodRunClient,
)
from tkr_cloud_video.core.errors import AppError


class _Response:
    """Minimal urlopen context manager returning a fixed JSON document."""

    def __init__(self, payload: object) -> None:
        self._buffer = BytesIO(json.dumps(payload).encode())

    def __enter__(self) -> BytesIO:
        return self._buffer

    def __exit__(self, *_: object) -> None:
        return None


def _answer(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> list[tuple[str, str, bytes | None]]:
    """Route urlopen to a fixed document, recording what was requested."""
    calls: list[tuple[str, str, bytes | None]] = []

    def _open(request: Any, **_: object) -> _Response:
        calls.append((request.method, request.full_url, request.data))
        return _Response(payload)

    monkeypatch.setattr(urllib.request, "urlopen", _open)
    return calls


def _fail(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    """Route urlopen to a failure."""

    def _open(*_: object, **__: object) -> Any:
        raise error

    monkeypatch.setattr(urllib.request, "urlopen", _open)


def client() -> RunPodRunClient:
    """Build a client for the configured endpoint."""
    return RunPodRunClient(settings(), CREDENTIALS)


@pytest.mark.asyncio
async def test_a_submission_goes_to_the_async_run_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never runsync: a cold worker hydrates before it accepts work."""
    calls = _answer(monkeypatch, {"id": "run-9", "status": "IN_QUEUE"})

    run_id = await client().submit({"input": {"seed": 7}})

    assert run_id == "run-9"
    method, url, body = calls[0]
    assert method == "POST"
    assert url == "https://api.runpod.ai/v2/176tpna3ogl94t/run"
    assert body is not None
    assert json.loads(body) == {"input": {"seed": 7}}


@pytest.mark.asyncio
async def test_a_paused_endpoint_is_a_state_to_wait_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raising a worker ceiling reaches configuration before the run plane."""
    _fail(
        monkeypatch,
        urllib.error.HTTPError(
            "https://api.runpod.ai",
            409,
            "Conflict",
            {},  # type: ignore[arg-type]
            BytesIO(b'{"error":"ENDPOINT_PAUSED"}'),
        ),
    )

    with pytest.raises(EndpointPausedError) as raised:
        await client().submit({"input": {}})

    assert raised.value.retryable is True


@pytest.mark.asyncio
async def test_a_submission_without_a_run_identifier_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider that accepts without naming a run leaves nothing to poll."""
    _answer(monkeypatch, {"status": "IN_QUEUE"})

    with pytest.raises(AppError, match="without naming a run"):
        await client().submit({"input": {}})


@pytest.mark.asyncio
async def test_an_observation_carries_the_handler_response_and_the_timings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The provider reports that the worker answered, not what it produced."""
    _answer(
        monkeypatch,
        {
            "status": "COMPLETED",
            "output": {"ok": True, "job_id": "job-1"},
            "delayTime": 7471,
            "executionTime": 739856,
        },
    )

    status = await client().status("run-9")

    assert status.terminal is True
    assert status.output == {"ok": True, "job_id": "job-1"}
    assert status.delay_time_ms == 7471
    assert status.execution_time_ms == 739856


@pytest.mark.asyncio
async def test_unusable_provider_timings_are_dropped_rather_than_coerced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A boolean is an integer in Python and must not read as one millisecond."""
    _answer(
        monkeypatch,
        {"status": "IN_PROGRESS", "delayTime": True, "executionTime": "soon"},
    )

    status = await client().status("run-9")

    assert status.delay_time_ms is None
    assert status.execution_time_ms is None
    assert status.terminal is False


@pytest.mark.asyncio
async def test_a_status_query_without_a_status_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An answer that names no status cannot be rendered as one."""
    _answer(monkeypatch, {"output": {}})

    with pytest.raises(AppError, match="without a status"):
        await client().status("run-9")


@pytest.mark.asyncio
async def test_a_cancellation_reports_the_status_that_followed_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation goes to the cancel route and answers with a status."""
    calls = _answer(monkeypatch, {"status": "CANCELLED"})

    assert await client().cancel("run-9") == "CANCELLED"
    assert calls[0][1].endswith("/cancel/run-9")


@pytest.mark.asyncio
async def test_endpoint_health_carries_only_validated_aggregate_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fleet telemetry uses the provider's bounded health document."""
    calls = _answer(
        monkeypatch,
        {
            "jobs": {
                "completed": 9,
                "failed": 1,
                "inProgress": 2,
                "inQueue": 3,
                "retried": 4,
            },
            "workers": {"idle": 1, "running": 2},
        },
    )

    health = await client().health()

    assert health == EndpointHealth(9, 1, 2, 3, 4, 1, 2)
    assert calls[0][:2] == (
        "GET",
        "https://api.runpod.ai/v2/176tpna3ogl94t/health",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"jobs": {}, "workers": {}},
        {
            "jobs": {
                "completed": True,
                "failed": 0,
                "inProgress": 0,
                "inQueue": 0,
                "retried": 0,
            },
            "workers": {"idle": 0, "running": 0},
        },
    ],
)
async def test_invalid_endpoint_health_is_refused(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    """Missing and non-numeric counts never become misleading zeroes."""
    _answer(monkeypatch, payload)

    with pytest.raises(AppError, match="health API"):
        await client().health()


@pytest.mark.asyncio
async def test_an_unreachable_provider_is_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transport failure says so, so the page can offer to try again."""
    _fail(monkeypatch, OSError("connection reset"))

    with pytest.raises(AppError) as raised:
        await client().status("run-9")

    assert raised.value.retryable is True


@pytest.mark.asyncio
async def test_a_document_that_is_not_an_object_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider answering with a list has nothing this client can read."""
    _answer(monkeypatch, ["not", "an", "object"])

    with pytest.raises(AppError, match="not an object"):
        await client().status("run-9")


@pytest.mark.asyncio
async def test_the_bearer_credential_is_sent_and_never_rendered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The credential reaches the provider and nothing else."""
    captured: list[Any] = []

    def _open(request: Any, **_: object) -> _Response:
        captured.append(request)
        return _Response({"id": "run-9"})

    monkeypatch.setattr(urllib.request, "urlopen", _open)
    built = client()

    await built.submit({"input": {}})

    assert captured[0].headers["Authorization"].endswith(CREDENTIALS.runpod_api_key)
    assert CREDENTIALS.runpod_api_key not in repr(CREDENTIALS)
