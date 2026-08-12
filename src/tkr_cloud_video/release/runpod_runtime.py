"""Opt-in, non-secret evidence for RunPod worker runtime diagnosis."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field

from tkr_cloud_video.core.context import validate_identifier
from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.delivery.uploader import ResultStore

RUNTIME_DIAGNOSTIC_ENVIRONMENT = "TKR_RUNTIME_DIAGNOSTIC_ID"
_WEBHOOK_VARIABLES = (
    "RUNPOD_WEBHOOK_GET_JOB",
    "RUNPOD_WEBHOOK_PING",
    "RUNPOD_WEBHOOK_POST_OUTPUT",
    "RUNPOD_WEBHOOK_POST_STREAM",
)


def normalize_runpod_runtime_environment(
    environment: MutableMapping[str, str],
) -> str | None:
    """Resolve one stable worker identity before importing the RunPod SDK.

    RunPod's SDK reads its worker identity at module import time and otherwise
    generates a random UUID. Deployed workers must instead use the provider's
    pod identity, falling back to the container hostname used by the runtime
    when the documented variable is absent. Every webhook is resolved from the
    same identity so queue intake, heartbeats, and results cannot drift.

    Args:
        environment: Mutable process environment used by the SDK import.

    Returns:
        The resolved deployed worker identity, or ``None`` for local execution.

    Raises:
        AppError: If a deployed worker lacks a safe, consistent identity.

    """
    if "RUNPOD_WEBHOOK_GET_JOB" not in environment:
        return None
    worker_id = environment.get("RUNPOD_POD_ID") or environment.get("HOSTNAME")
    if worker_id is None:
        raise AppError(
            "runpod_worker_identity_missing",
            "RunPod worker identity is unavailable.",
        )
    try:
        validated_worker_id = validate_identifier(worker_id, "runpod_pod_id")
    except AppError as error:
        raise AppError(
            "runpod_worker_identity_invalid",
            "RunPod worker identity is invalid.",
            cause=error,
        ) from error

    resolved_webhooks: dict[str, str] = {}
    for name in _WEBHOOK_VARIABLES:
        webhook = environment.get(name)
        if webhook is None:
            raise AppError(
                "runpod_webhook_missing",
                "A required RunPod worker webhook is unavailable.",
                context={"field": name},
            )
        resolved = webhook.replace("$RUNPOD_POD_ID", validated_worker_id)
        if validated_worker_id not in resolved:
            raise AppError(
                "runpod_webhook_identity_missing",
                "A RunPod worker webhook lacks the resolved identity.",
                context={"field": name},
            )
        resolved_webhooks[name] = resolved

    environment["RUNPOD_POD_ID"] = validated_worker_id
    environment.update(resolved_webhooks)
    return validated_worker_id


@dataclass(frozen=True, slots=True)
class RunPodRuntimeDiagnostic:
    """Safe runtime identity and webhook-shape evidence from one container."""

    diagnostic_id: str
    release_id: str
    runpod_pod_id: str | None
    runpod_pod_hostname: str | None
    container_hostname: str | None
    endpoint_id: str | None
    webhook_has_pod_placeholder: dict[str, bool]

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str]
    ) -> RunPodRuntimeDiagnostic | None:
        """Build evidence only when an operator supplies a diagnostic ID."""
        diagnostic_id = environment.get(RUNTIME_DIAGNOSTIC_ENVIRONMENT)
        if diagnostic_id is None:
            return None
        validate_identifier(diagnostic_id, "diagnostic_id")
        release_id = validate_identifier(
            environment.get("TKR_RELEASE_ID", ""), "release_id"
        )
        return cls(
            diagnostic_id=diagnostic_id,
            release_id=release_id,
            runpod_pod_id=_optional_identifier(
                environment.get("RUNPOD_POD_ID"), "runpod_pod_id"
            ),
            runpod_pod_hostname=_optional_identifier(
                environment.get("RUNPOD_POD_HOSTNAME"), "runpod_pod_hostname"
            ),
            container_hostname=_optional_identifier(
                environment.get("HOSTNAME"), "container_hostname"
            ),
            endpoint_id=_optional_identifier(
                environment.get("RUNPOD_ENDPOINT_ID"), "endpoint_id"
            ),
            webhook_has_pod_placeholder={
                name: "$RUNPOD_POD_ID" in environment.get(name, "")
                for name in _WEBHOOK_VARIABLES
            },
        )

    def canonical_bytes(self) -> bytes:
        """Serialize deterministic evidence without ambient environment data."""
        return json.dumps(
            {
                "container_hostname": self.container_hostname,
                "diagnostic_id": self.diagnostic_id,
                "endpoint_id": self.endpoint_id,
                "release_id": self.release_id,
                "runpod_pod_hostname": self.runpod_pod_hostname,
                "runpod_pod_id": self.runpod_pod_id,
                "schema_version": "1",
                "webhook_has_pod_placeholder": self.webhook_has_pod_placeholder,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @property
    def object_key(self) -> str:
        """Return the immutable, operator-selected diagnostic object key."""
        return f"runtime-diagnostics/{self.diagnostic_id}.json"


@dataclass(slots=True)
class RunPodRuntimeDiagnosticPublisher:
    """Publish one immutable runtime diagnostic before handling a job."""

    store: ResultStore = field(repr=False)
    diagnostic: RunPodRuntimeDiagnostic
    _lock: asyncio.Lock = field(init=False, repr=False)
    _published: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        """Create the one-process publication gate."""
        self._lock = asyncio.Lock()

    async def publish(self) -> None:
        """Create the diagnostic once and tolerate an existing exact object."""
        async with self._lock:
            if self._published:
                return
            content = self.diagnostic.canonical_bytes()
            digest = hashlib.sha256(content).hexdigest()
            current = await self.store.head(self.diagnostic.object_key)
            if current is None:
                await self.store.put(self.diagnostic.object_key, content, digest)
            elif current.size_bytes != len(content) or current.sha256 != digest:
                raise ValueError("runtime diagnostic identity already has other data")
            self._published = True


def _optional_identifier(value: str | None, field_name: str) -> str | None:
    """Validate an optional provider identity before persisting it."""
    if value is None:
        return None
    return validate_identifier(value, field_name)
