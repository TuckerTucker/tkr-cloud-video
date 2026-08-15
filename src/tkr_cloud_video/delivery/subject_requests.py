"""Principal-facing access and erasure for one generation record.

The job identity is a one-way SHA-256 over principal and request hash, so a
principal's records cannot be enumerated from the principal alone. That is a
property worth keeping: a job id discloses nothing about who submitted it. The
cost is that a subject request is scoped to a record the principal presents
rather than to everything held about them, and this module answers "do you hold
this" rather than "tell me everything you hold".

Nothing here writes. Recomputing the identity from caller-presented content and
discarding it means serving a subject request creates no new linkage between a
principal and a job.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.delivery.access_policy import ResultAccessPolicy
from tkr_cloud_video.delivery.lifecycle import LifecyclePolicy, RetainedClass
from tkr_cloud_video.delivery.reconciler import COMMIT_MARKER, RetentionStore

VIDEO_FIELD = "output_video"
PROMPT_FIELD = "prompt"
INPUT_FIELD = "input_reference"


class SubjectRequestError(AppError):
    """A subject access or erasure request could not be served."""


@dataclass(frozen=True, slots=True)
class SubjectRecord:
    """What is held for one record, and when it is scheduled to go."""

    job_id: str
    attempt_id: str
    held_fields: tuple[str, ...]
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ErasureReceipt:
    """Outcome of one erasure, honest about what survived."""

    job_id: str
    erased: tuple[str, ...]
    unreachable: tuple[str, ...]

    @property
    def complete(self) -> bool:
        """Return whether every object was reached."""
        return not self.unreachable


class SubjectRequestService:
    """Serves access and erasure for a record the principal presents."""

    def __init__(
        self,
        access: ResultAccessPolicy,
        store: RetentionStore,
        policy: LifecyclePolicy,
    ) -> None:
        """Initialize with authorization, a delete-capable store, and policy."""
        self._access = access
        self._store = store
        self._policy = policy

    async def describe(
        self, principal_id: str, request_hash: str
    ) -> SubjectRecord | None:
        """Report what is held for one record and when it expires.

        Takes no reference instant: the expiry it reports is absolute, derived
        from when the record was written plus its class period, so the answer
        does not depend on when it is asked.

        Args:
            principal_id: The requesting principal.
            request_hash: Hash of the request the principal presents.

        Returns:
            The record, or None when the caller is not authorised for it or it
            does not exist. Those two share one outcome deliberately, so a
            subject request cannot be used to probe for existence.

        Raises:
            SubjectRequestError: If the store cannot be reached. Reporting an
                empty holding on a failure would be an answer a principal acts
                on, so unavailability must not look like absence.

        """
        job_id, attempt_id = identity_for(principal_id, request_hash)
        if not await self._access.authorize(principal_id, job_id):
            return None
        prefix = f"outputs/{job_id}/{attempt_id}/"
        try:
            objects = await self._store.list_objects(prefix)
        except Exception as error:
            raise SubjectRequestError(
                "subject_store_unavailable",
                "the record store could not be reached",
                retryable=True,
                cause=error,
            ) from error
        if not objects:
            return None
        held = [VIDEO_FIELD, PROMPT_FIELD]
        oldest = min(item.uploaded_at for item in objects)
        committed = any(item.key.endswith(COMMIT_MARKER) for item in objects)
        governing = (
            RetainedClass.DELIVERABLE if committed else RetainedClass.FAILED_ATTEMPT
        )
        return SubjectRecord(
            job_id=job_id,
            attempt_id=attempt_id,
            held_fields=tuple(held),
            expires_at=oldest + self._policy.period_for(governing),
        )

    async def erase(
        self,
        principal_id: str,
        request_hash: str,
        input_keys: tuple[str, ...] = (),
    ) -> ErasureReceipt | None:
        """Erase one record's outputs and the input references supplied.

        Args:
            principal_id: The requesting principal.
            request_hash: Hash of the request the principal presents.
            input_keys: Input object references the principal supplies, since
                they are named by the request rather than discoverable from
                the job identity.

        Returns:
            A receipt naming what was erased and what could not be reached, or
            None when the caller is not authorised or the record is absent.

        """
        job_id, attempt_id = identity_for(principal_id, request_hash)
        if not await self._access.authorize(principal_id, job_id):
            return None
        prefix = f"outputs/{job_id}/{attempt_id}/"
        try:
            objects = await self._store.list_objects(prefix)
        except Exception as error:
            raise SubjectRequestError(
                "subject_store_unavailable",
                "the record store could not be reached",
                retryable=True,
                cause=error,
            ) from error
        if not objects:
            return None
        erased: list[str] = []
        unreachable: list[str] = []
        # The commit marker goes last for the same reason the reconciler orders
        # it last: an interrupted erasure leaves a record that still reads as
        # committed rather than as a failed attempt on a shorter clock.
        ordered = [item.key for item in objects if not item.key.endswith(COMMIT_MARKER)]
        ordered.extend(item.key for item in objects if item.key.endswith(COMMIT_MARKER))
        for key in (*input_keys, *ordered):
            try:
                await self._store.delete(key)
            except Exception:
                unreachable.append(_field_for(key))
                continue
            erased.append(_field_for(key))
        return ErasureReceipt(
            job_id=job_id,
            erased=tuple(dict.fromkeys(erased)),
            unreachable=tuple(dict.fromkeys(unreachable)),
        )


def identity_for(principal_id: str, request_hash: str) -> tuple[str, str]:
    """Recompute the job and attempt identity for a presented request.

    Mirrors the derivation in the generation path exactly. Any divergence would
    make a principal unable to reach their own record, so this is the one place
    the shape is duplicated and it is pinned by test.

    Args:
        principal_id: The principal that submitted the request.
        request_hash: Canonical hash of the request.

    Returns:
        The job id and attempt id for that pairing.

    """
    digest = hashlib.sha256(f"{principal_id}:{request_hash}".encode()).hexdigest()
    return f"job-{digest[:32]}", f"attempt-{digest[32:]}"


def _field_for(key: str) -> str:
    """Map an object key to the personal field it carries."""
    if key.startswith("inputs/"):
        return INPUT_FIELD
    if key.endswith("generation.bin"):
        return PROMPT_FIELD
    return VIDEO_FIELD
