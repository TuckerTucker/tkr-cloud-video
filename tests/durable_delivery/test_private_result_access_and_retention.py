"""Private access, signed-link, lifecycle, and composition tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from tests.conftest import FakeClock
from tests.durable_delivery.test_verified_result_commit import (
    MemoryResultStore,
    manifest,
)
from tkr_cloud_video.delivery.access_policy import (
    ResultAccessPolicy,
    ResultAuthorizer,
    ResultState,
)
from tkr_cloud_video.delivery.api import PrivateResultService, ResultRepository
from tkr_cloud_video.delivery.contracts import (
    RemoteArtifact,
    ResultManifest,
    ResultRole,
)
from tkr_cloud_video.delivery.lifecycle import (
    LifecyclePolicy,
    RetainedClass,
    RetentionRule,
)
from tkr_cloud_video.delivery.signed_links import LinkSigner, SignedLinkService
from tkr_cloud_video.durable_delivery.composition import (
    DeliveryDependencies,
    compose_durable_delivery,
)


@dataclass
class Authorizer(ResultAuthorizer):
    """Configurable result authorization fake."""

    allow: bool

    async def allowed(self, principal_id: str, job_id: str) -> bool:
        """Return configured decision without revealing repository state."""
        assert principal_id and job_id
        return self.allow


@dataclass
class Repository(ResultRepository):
    """Committed result repository fake."""

    result: ResultManifest
    lookups: int = 0

    async def state(self, job_id: str) -> ResultState:
        """Return completed for the known synthetic job."""
        self.lookups += 1
        return ResultState.COMPLETED if job_id == "job-1" else ResultState.NOT_FOUND

    async def manifest(self, job_id: str) -> ResultManifest:
        """Return the committed synthetic manifest."""
        assert job_id == "job-1"
        return self.result


@dataclass
class Signer(LinkSigner):
    """Delivery-only signer recording bounded TTL."""

    calls: list[tuple[str, int]]

    async def sign(self, object_key: str, ttl_seconds: int) -> str:
        """Return a synthetic URL containing an opaque marker."""
        self.calls.append((object_key, ttl_seconds))
        return f"https://private.invalid/{object_key}?opaque=marker"


def committed_manifest() -> ResultManifest:
    """Build a complete committed manifest without remote writes."""
    evidence = tuple(
        RemoteArtifact(
            role=role,
            remote_key=f"outputs/job-1/attempt-1/{role.value}.bin",
            size_bytes=10,
            sha256=character * 64,
            provider_version_id=f"version-{index}",
        )
        for index, (role, character) in enumerate(
            zip(ResultRole, ("a", "b", "c"), strict=True), start=1
        )
    )
    return manifest(evidence)


@pytest.mark.asyncio
async def test_unauthorized_lookup_does_not_touch_repository() -> None:
    """Existing and absent jobs are indistinguishable without authorization."""
    repository = Repository(committed_manifest())
    service = PrivateResultService(
        ResultAccessPolicy(Authorizer(False)),
        repository,
        SignedLinkService(Signer([]), FakeClock(), 300),
    )
    response = await service.lookup("principal-1", "job-1")
    assert response.state is ResultState.NOT_FOUND
    assert repository.lookups == 0


@pytest.mark.asyncio
async def test_authorized_download_is_scoped_and_ttl_bounded() -> None:
    """Only committed video receives a delivery link capped by policy."""
    result = committed_manifest()
    signer = Signer([])
    service = PrivateResultService(
        ResultAccessPolicy(Authorizer(True)),
        Repository(result),
        SignedLinkService(signer, FakeClock(), 300),
    )
    link = await service.download("principal-1", "job-1", 1000)
    assert link is not None and signer.calls[0][1] == 300
    assert signer.calls[0][0].endswith("video.bin")
    assert link.expires_at == FakeClock().now() + timedelta(seconds=300)


def lifecycle_policy() -> LifecyclePolicy:
    """Build a complete policy with short non-deliverable retention."""
    return LifecyclePolicy(
        "1",
        tuple(
            RetentionRule(item, 365 if item is RetainedClass.DELIVERABLE else 7)
            for item in RetainedClass
        ),
    )


def test_lifecycle_expires_failed_attempt_but_retains_deliverable() -> None:
    """Class-specific cleanup never implies deleting retained delivery objects.

    This asserts on the predicate only. It is deliberately not evidence that
    anything is deleted; slice 15 carries the coverage that asserts on storage
    state after a period elapses.
    """
    policy = lifecycle_policy()
    created = datetime(2026, 1, 1, tzinfo=UTC)
    evaluated = created + timedelta(days=8)
    assert policy.expired(RetainedClass.FAILED_ATTEMPT, created, evaluated)
    assert not policy.expired(RetainedClass.DELIVERABLE, created, evaluated)


def test_composition_wires_separate_store_authorization_and_signer() -> None:
    """Delivery composition keeps upload and delivery capabilities injectable."""
    result = committed_manifest()
    services = compose_durable_delivery(
        DeliveryDependencies(
            MemoryResultStore(),
            Authorizer(True),
            Repository(result),
            Signer([]),
            FakeClock(),
            300,
            lifecycle_policy(),
        )
    )
    assert services.uploader is not None
    assert services.committer is not None
    assert services.private_results is not None
