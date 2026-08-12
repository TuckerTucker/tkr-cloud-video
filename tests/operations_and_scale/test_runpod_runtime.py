"""RunPod runtime evidence tests."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

import pytest

from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.delivery.uploader import RemoteMetadata
from tkr_cloud_video.release.runpod_runtime import (
    RunPodRuntimeDiagnostic,
    RunPodRuntimeDiagnosticPublisher,
    normalize_runpod_runtime_environment,
)


@dataclass
class Store:
    """In-memory immutable diagnostic store."""

    objects: dict[str, bytes] = field(default_factory=dict)

    async def head(self, key: str) -> RemoteMetadata | None:
        """Return evidence for existing bytes."""
        content = self.objects.get(key)
        if content is None:
            return None
        digest = hashlib.sha256(content).hexdigest()
        return RemoteMetadata(len(content), digest, f"sha256-{digest}")

    async def put(self, key: str, content: bytes, sha256: str) -> RemoteMetadata:
        """Create one immutable object."""
        assert key not in self.objects
        assert hashlib.sha256(content).hexdigest() == sha256
        self.objects[key] = content
        return RemoteMetadata(len(content), sha256, f"sha256-{sha256}")

    async def get(self, key: str) -> bytes | None:
        """Return stored bytes."""
        return self.objects.get(key)


def runtime_environment() -> dict[str, str]:
    """Return a safe synthetic RunPod environment."""
    return {
        "TKR_RUNTIME_DIAGNOSTIC_ID": "release-1-probe",
        "TKR_RELEASE_ID": "release-1",
        "RUNPOD_POD_ID": "pod-1",
        "RUNPOD_POD_HOSTNAME": "pod-1.internal",
        "HOSTNAME": "container-1",
        "RUNPOD_ENDPOINT_ID": "endpoint-1",
        "RUNPOD_WEBHOOK_GET_JOB": "https://example/job/$RUNPOD_POD_ID",
        "RUNPOD_WEBHOOK_POST_OUTPUT": "https://example/done/$RUNPOD_POD_ID/$ID",
        "RUNPOD_AI_API_KEY": "must-not-persist",
    }


def test_runtime_normalization_is_inert_outside_runpod() -> None:
    """Local execution never invents provider configuration."""
    environment = {"HOSTNAME": "local-container"}

    assert normalize_runpod_runtime_environment(environment) is None
    assert environment == {"HOSTNAME": "local-container"}


@pytest.mark.parametrize("use_documented_identity", [False, True])
def test_runtime_normalization_resolves_every_webhook_from_one_identity(
    use_documented_identity: bool,
) -> None:
    """All SDK transports share the documented ID or container fallback."""
    environment = runtime_environment()
    expected = "pod-1" if use_documented_identity else "container-1"
    if not use_documented_identity:
        environment.pop("RUNPOD_POD_ID")
    for name in (
        "RUNPOD_WEBHOOK_PING",
        "RUNPOD_WEBHOOK_POST_STREAM",
    ):
        environment[name] = f"https://example/{name}/$RUNPOD_POD_ID"

    assert normalize_runpod_runtime_environment(environment) == expected
    assert environment["RUNPOD_POD_ID"] == expected
    assert all(
        expected in environment[name] and "$RUNPOD_POD_ID" not in environment[name]
        for name in (
            "RUNPOD_WEBHOOK_GET_JOB",
            "RUNPOD_WEBHOOK_PING",
            "RUNPOD_WEBHOOK_POST_OUTPUT",
            "RUNPOD_WEBHOOK_POST_STREAM",
        )
    )


@pytest.mark.parametrize(
    ("change", "error_code"),
    [
        ({"RUNPOD_POD_ID": "unsafe/id"}, "runpod_worker_identity_invalid"),
        ({"RUNPOD_WEBHOOK_PING": None}, "runpod_webhook_missing"),
        (
            {"RUNPOD_WEBHOOK_PING": "https://example/ping"},
            "runpod_webhook_identity_missing",
        ),
    ],
)
def test_runtime_normalization_fails_closed_on_identity_drift(
    change: dict[str, str | None], error_code: str
) -> None:
    """Malformed identities and inconsistent webhooks stop SDK startup."""
    environment = runtime_environment()
    environment["RUNPOD_WEBHOOK_PING"] = "https://example/ping/$RUNPOD_POD_ID"
    environment["RUNPOD_WEBHOOK_POST_STREAM"] = "https://example/stream/$RUNPOD_POD_ID"
    for name, value in change.items():
        if value is None:
            environment.pop(name, None)
        else:
            environment[name] = value

    with pytest.raises(AppError) as captured:
        normalize_runpod_runtime_environment(environment)

    assert captured.value.code == error_code


def test_runtime_diagnostic_is_opt_in_and_contains_only_safe_evidence() -> None:
    """The probe ignores all ambient variables except its explicit allowlist."""
    assert RunPodRuntimeDiagnostic.from_environment({}) is None

    diagnostic = RunPodRuntimeDiagnostic.from_environment(runtime_environment())

    assert diagnostic is not None
    payload = json.loads(diagnostic.canonical_bytes())
    assert payload["runpod_pod_id"] == "pod-1"
    assert payload["container_hostname"] == "container-1"
    assert payload["webhook_has_pod_placeholder"] == {
        "RUNPOD_WEBHOOK_GET_JOB": True,
        "RUNPOD_WEBHOOK_PING": False,
        "RUNPOD_WEBHOOK_POST_OUTPUT": True,
        "RUNPOD_WEBHOOK_POST_STREAM": False,
    }
    assert "must-not-persist" not in diagnostic.canonical_bytes().decode()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("TKR_RUNTIME_DIAGNOSTIC_ID", "../probe"),
        ("RUNPOD_POD_ID", "pod/escape"),
        ("HOSTNAME", "host\nforged"),
    ],
)
def test_runtime_diagnostic_rejects_unsafe_provider_identity(
    name: str, value: str
) -> None:
    """Unsafe provider values never become object keys or evidence bytes."""
    environment = runtime_environment()
    environment[name] = value

    with pytest.raises(AppError):
        RunPodRuntimeDiagnostic.from_environment(environment)


@pytest.mark.asyncio
async def test_runtime_diagnostic_publishes_once_and_reuses_exact_evidence() -> None:
    """Concurrent worker calls cannot overwrite diagnostic evidence."""
    diagnostic = RunPodRuntimeDiagnostic.from_environment(runtime_environment())
    assert diagnostic is not None
    store = Store()
    publisher = RunPodRuntimeDiagnosticPublisher(store, diagnostic)

    await publisher.publish()
    await publisher.publish()

    assert list(store.objects) == ["runtime-diagnostics/release-1-probe.json"]
    assert store.objects[diagnostic.object_key] == diagnostic.canonical_bytes()

    second_process = RunPodRuntimeDiagnosticPublisher(store, diagnostic)
    await second_process.publish()
    assert len(store.objects) == 1


@pytest.mark.asyncio
async def test_runtime_diagnostic_rejects_conflicting_existing_evidence() -> None:
    """A diagnostic ID cannot silently alias a different worker snapshot."""
    diagnostic = RunPodRuntimeDiagnostic.from_environment(runtime_environment())
    assert diagnostic is not None
    store = Store({diagnostic.object_key: b"different"})

    with pytest.raises(ValueError, match="other data"):
        await RunPodRuntimeDiagnosticPublisher(store, diagnostic).publish()
