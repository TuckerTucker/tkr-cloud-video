"""Subject access and erasure coverage for slice 14.

Erasure is proven by absence from the store, never by a receipt field alone,
and non-enumeration is asserted by comparing the unauthorised and absent
outcomes directly rather than by checking both are merely falsy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from tests.durable_delivery.test_retention_reconciler import (
    COMPLETE,
    MemoryRetentionStore,
    attempt,
    policy,
)
from tkr_cloud_video.delivery.access_policy import ResultAccessPolicy, ResultAuthorizer
from tkr_cloud_video.delivery.lifecycle import RetainedClass
from tkr_cloud_video.delivery.reconciler import StoredObject
from tkr_cloud_video.delivery.subject_requests import (
    INPUT_FIELD,
    PROMPT_FIELD,
    VIDEO_FIELD,
    SubjectRequestError,
    SubjectRequestService,
    identity_for,
)

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
PRINCIPAL = "principal-1"
REQUEST = "a" * 64


@dataclass
class Authorizer(ResultAuthorizer):
    """Authorization fake keyed to one principal."""

    allow: bool

    async def allowed(self, principal_id: str, job_id: str) -> bool:
        """Return the configured decision."""
        assert principal_id and job_id
        return self.allow


class BrokenStore(MemoryRetentionStore):
    """Store whose listing fails."""

    async def list_objects(self, prefix: str) -> tuple[StoredObject, ...]:
        """Fail rather than report an empty holding."""
        raise RuntimeError("provider unavailable")


def held_store() -> MemoryRetentionStore:
    """Build a store holding one committed attempt for the principal."""
    job_id, attempt_id = identity_for(PRINCIPAL, REQUEST)
    objects = {f"outputs/{job_id}/{attempt_id}/{role}": EPOCH for role in COMPLETE}
    objects["inputs/reference-1.png"] = EPOCH
    return MemoryRetentionStore(objects)


def service(store: MemoryRetentionStore, *, allow: bool) -> SubjectRequestService:
    """Compose the service over fakes."""
    return SubjectRequestService(ResultAccessPolicy(Authorizer(allow)), store, policy())


def test_identity_matches_the_generation_path() -> None:
    """The recomputed identity must equal what submission derived.

    A divergence here makes a principal unable to reach their own record, so
    the shape is pinned rather than trusted to stay in step.
    """
    job_id, attempt_id = identity_for(PRINCIPAL, REQUEST)
    assert job_id.startswith("job-") and len(job_id) == 36
    assert attempt_id.startswith("attempt-") and len(attempt_id) == 40
    assert identity_for(PRINCIPAL, REQUEST) == (job_id, attempt_id)
    assert identity_for("principal-2", REQUEST)[0] != job_id


@pytest.mark.asyncio
async def test_authorised_principal_learns_what_is_held_and_when_it_goes() -> None:
    """S14-T01: the answer states the fields and the scheduled expiry."""
    record = await service(held_store(), allow=True).describe(PRINCIPAL, REQUEST)
    assert record is not None
    assert set(record.held_fields) == {VIDEO_FIELD, PROMPT_FIELD}
    assert record.expires_at == EPOCH + policy().period_for(RetainedClass.DELIVERABLE)


@pytest.mark.asyncio
async def test_unauthorised_is_indistinguishable_from_absent() -> None:
    """S14-T02: refusal and absence are one outcome, compared directly."""
    refused = await service(held_store(), allow=False).describe(PRINCIPAL, REQUEST)
    absent = await service(MemoryRetentionStore({}), allow=True).describe(
        PRINCIPAL, REQUEST
    )
    assert refused == absent


@pytest.mark.asyncio
async def test_erasure_reaches_video_prompt_and_input_together() -> None:
    """S14-T03: absence in the store is the proof, not the receipt."""
    store = held_store()
    receipt = await service(store, allow=True).erase(
        PRINCIPAL, REQUEST, ("inputs/reference-1.png",)
    )
    assert receipt is not None and receipt.complete
    assert store.objects == {}
    assert set(receipt.erased) == {VIDEO_FIELD, PROMPT_FIELD, INPUT_FIELD}


@pytest.mark.asyncio
async def test_partial_erasure_reports_itself_partial() -> None:
    """S14-T04: what survived is named, and the call does not raise."""
    store = held_store()
    job_id, attempt_id = identity_for(PRINCIPAL, REQUEST)
    store.refuse = {f"outputs/{job_id}/{attempt_id}/generation.bin"}
    receipt = await service(store, allow=True).erase(PRINCIPAL, REQUEST)
    assert receipt is not None
    assert not receipt.complete
    assert receipt.unreachable == (PROMPT_FIELD,)
    assert f"outputs/{job_id}/{attempt_id}/generation.bin" in store.objects


@pytest.mark.asyncio
async def test_erasure_removes_the_commit_marker_last() -> None:
    """An interrupted erasure must not leave a shorter-clock record."""
    store = held_store()
    await service(store, allow=True).erase(PRINCIPAL, REQUEST)
    assert store.deleted[-1].endswith("result.json")


@pytest.mark.asyncio
async def test_unavailable_store_never_reads_as_nothing_held() -> None:
    """S14-T05: unavailability must not be reported as absence."""
    with pytest.raises(SubjectRequestError) as caught:
        await service(BrokenStore({}), allow=True).describe(PRINCIPAL, REQUEST)
    assert caught.value.code == "subject_store_unavailable"
    assert caught.value.retryable


@pytest.mark.asyncio
async def test_unauthorised_erasure_deletes_nothing() -> None:
    """Authorization gates the delete capability, not only the answer."""
    store = held_store()
    assert await service(store, allow=False).erase(PRINCIPAL, REQUEST) is None
    assert store.deleted == []


@pytest.mark.asyncio
async def test_failed_attempt_expiry_uses_the_shorter_period() -> None:
    """An uncommitted record reports the failed-attempt period."""
    job_id, attempt_id = identity_for(PRINCIPAL, REQUEST)
    store = MemoryRetentionStore(
        {
            f"outputs/{job_id}/{attempt_id}/video.bin": EPOCH,
            f"outputs/{job_id}/{attempt_id}/generation.bin": EPOCH,
        }
    )
    record = await service(store, allow=True).describe(PRINCIPAL, REQUEST)
    assert record is not None
    assert record.expires_at == EPOCH + timedelta(days=14)


def test_attempt_helper_is_shared_with_the_reconciler_suite() -> None:
    """The two suites agree on what an attempt namespace looks like."""
    assert attempt("job-1", COMPLETE, EPOCH)
