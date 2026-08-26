"""Deterministic fakes for the console's provider and storage boundaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from tkr_cloud_video.console.run_client import EndpointHealth, RunStatus
from tkr_cloud_video.console.settings import ConsoleCredentials, ConsoleSettings

# The instant the frozen presigning vector in test_signed_delivery.py was
# computed at. Every console test dates from it so a signature is reproducible.
SIGNING_INSTANT = datetime(2026, 8, 23, 12, 34, 56, tzinfo=UTC)

CREDENTIALS = ConsoleCredentials(
    runpod_api_key="rpa_ExampleRunPodKeyValue00",
    delivery_key_id="0026abcdef0123456789abcd",
    delivery_application_key="K002exampleSecretValue0123456789ab",
)


@dataclass(frozen=True)
class SigningClock:
    """Clock frozen at the instant the signing vectors were computed."""

    current: datetime = SIGNING_INSTANT

    def now(self) -> datetime:
        """Return the frozen instant."""
        return self.current

    def monotonic(self) -> float:
        """Return a fixed monotonic reading."""
        return 100.0


def settings(**overrides: Any) -> ConsoleSettings:
    """Build console settings for a test, with the reviewed defaults."""
    values: dict[str, Any] = {
        "endpoint_id": "176tpna3ogl94t",
        "principal_id": "runpod-endpoint",
        "bucket_name": "tkr-video-ca",
    }
    values.update(overrides)
    return ConsoleSettings(**values)


@dataclass
class FakeRunClient:
    """Records submissions and answers configured statuses."""

    run_id: str = "run-1"
    statuses: dict[str, RunStatus] = field(default_factory=dict)
    submitted: list[dict[str, Any]] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    submit_error: Exception | None = None
    fail_with: Exception | None = None
    endpoint_health: EndpointHealth = field(
        default_factory=lambda: EndpointHealth(1, 0, 0, 0, 0, 1, 0)
    )

    async def submit(self, payload: dict[str, Any]) -> str:
        """Record one submission, or raise the configured failure."""
        if self.submit_error is not None:
            raise self.submit_error
        self.submitted.append(payload)
        return self.run_id

    async def status(self, run_id: str) -> RunStatus:
        """Return the configured observation for a run, or the configured failure."""
        if self.fail_with is not None:
            raise self.fail_with
        return self.statuses.get(run_id, RunStatus(run_id=run_id, status="IN_QUEUE"))

    async def cancel(self, run_id: str) -> str:
        """Record one cancellation, or raise the configured failure."""
        if self.fail_with is not None:
            raise self.fail_with
        self.cancelled.append(run_id)
        return "CANCELLED"

    async def health(self) -> EndpointHealth:
        """Return aggregate health, or the configured failure."""
        if self.fail_with is not None:
            raise self.fail_with
        return self.endpoint_health


@dataclass
class FakeObjectReader:
    """Serves object bytes from an in-memory bucket."""

    objects: dict[str, bytes] = field(default_factory=dict)
    reads: list[str] = field(default_factory=list)
    fail_with: Exception | None = None

    async def get(self, object_key: str) -> bytes | None:
        """Return the stored bytes, or None when nothing is stored."""
        self.reads.append(object_key)
        if self.fail_with is not None:
            raise self.fail_with
        return self.objects.get(object_key)
