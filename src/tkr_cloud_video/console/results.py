"""Session job registry, its authorizer, and the committed-result repository.

Authorization here is structural rather than asserted. A job's identity is a
digest of the principal and the canonical request hash, so the console can only
name a job whose request it holds. A job id typed into the address bar resolves
to nothing, because nothing derived it, which is the same outcome the delivery
policy already gives an unauthorized caller.

The registry is in memory and lives as long as the process. Restarting the
console forgets which jobs it submitted, and that is the intended trade: the
alternative is a new on-disk record of job identities and request hashes with
no retention clock, which is the gap `.claude/rules/compliance-triage.md`
already records against the fields this product does persist.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

from pydantic import ValidationError

from tkr_cloud_video.console.objects import ObjectReader
from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.delivery.access_policy import ResultState
from tkr_cloud_video.delivery.contracts import ResultManifest


def derive_identity(principal_id: str, request_hash: str) -> tuple[str, str]:
    """Return the job and attempt identity the generation path would mint.

    This mirrors ``application._job_identity`` and
    ``delivery.subject_requests.identity_for`` exactly. It is duplicated for the
    same reason those two are: a console that derived identities differently
    would look for a committed result under a key the worker never wrote, and
    report every successful generation as missing. The agreement is pinned by
    test rather than by import, so a change to either side fails CI.

    Args:
        principal_id: The principal the endpoint generates as.
        request_hash: The canonical request hash.

    Returns:
        The job id and attempt id for that pairing.

    """
    digest = hashlib.sha256(f"{principal_id}:{request_hash}".encode()).hexdigest()
    return f"job-{digest[:32]}", f"attempt-{digest[32:]}"


@dataclass(frozen=True, slots=True)
class JobRecord:
    """One generation this console submitted, as the console can see it.

    Args:
        job_id: The derived job identity, which is also what the worker
            returns, so a mismatch between them is detectable.
        attempt_id: The derived attempt identity, which completes the key of
            the committed result marker.
        request_hash: The canonical request hash the identity came from.
        run_id: The RunPod run this was submitted as.
        submitted_at: When the console submitted it.
        model_set_id: The model set generated with, for display only.
        below_trained_envelope: Whether the request sits under the model's
            trained range, so the console can say so beside the result.

    """

    job_id: str
    attempt_id: str
    request_hash: str
    run_id: str
    submitted_at: datetime
    model_set_id: str
    below_trained_envelope: bool

    @property
    def commit_key(self) -> str:
        """Return the exact key the result marker would be committed under."""
        return f"outputs/{self.job_id}/{self.attempt_id}/result.json"


class JobRegistry:
    """Thread-safe record of what this console session submitted.

    Requests arrive on the server's worker threads, so the guard is a threading
    lock rather than an asyncio one: each request runs its own event loop, and a
    lock bound to one loop cannot be awaited from another.
    """

    def __init__(self, principal_id: str) -> None:
        """Initialize for the one principal this console submits as."""
        self._principal_id = principal_id
        self._records: dict[str, JobRecord] = {}
        self._by_run: dict[str, str] = {}
        self._lock = threading.Lock()

    @property
    def principal_id(self) -> str:
        """Return the principal every record here belongs to."""
        return self._principal_id

    def register(
        self,
        *,
        request_hash: str,
        run_id: str,
        submitted_at: datetime,
        model_set_id: str,
        below_trained_envelope: bool,
    ) -> JobRecord:
        """Derive and record the identity of one submitted generation."""
        job_id, attempt_id = derive_identity(self._principal_id, request_hash)
        record = JobRecord(
            job_id=job_id,
            attempt_id=attempt_id,
            request_hash=request_hash,
            run_id=run_id,
            submitted_at=submitted_at,
            model_set_id=model_set_id,
            below_trained_envelope=below_trained_envelope,
        )
        with self._lock:
            self._records[job_id] = record
            self._by_run[run_id] = job_id
        return record

    def by_job(self, job_id: str) -> JobRecord | None:
        """Return the record for a job id, or None when none was derived."""
        with self._lock:
            return self._records.get(job_id)

    def by_run(self, run_id: str) -> JobRecord | None:
        """Return the record for a RunPod run id."""
        with self._lock:
            job_id = self._by_run.get(run_id)
            return None if job_id is None else self._records.get(job_id)

    def __iter__(self) -> Iterator[JobRecord]:
        """Iterate a stable snapshot, newest submission first."""
        with self._lock:
            records = list(self._records.values())
        return iter(sorted(records, key=lambda r: r.submitted_at, reverse=True))


class RegisteredJobAuthorizer:
    """Authorizes only a job this console derived for its own principal."""

    def __init__(self, registry: JobRegistry) -> None:
        """Initialize against the session registry."""
        self._registry = registry

    async def allowed(self, principal_id: str, job_id: str) -> bool:
        """Return whether this principal derived this job id here."""
        if principal_id != self._registry.principal_id:
            return False
        return self._registry.by_job(job_id) is not None


class AttemptResultRepository:
    """Reads committed state and metadata for a registered job.

    The repository answers only what the delivery bucket knows: a job is
    ``COMPLETED`` once its result marker exists, and ``IN_PROGRESS`` until then.
    It never reports ``FAILED``, because a bucket cannot distinguish a run that
    failed from one still sampling. The console overlays that from the run's own
    terminal status, which is the only place it is known.
    """

    def __init__(self, registry: JobRegistry, reader: ObjectReader) -> None:
        """Initialize with the session registry and the object reader."""
        self._registry = registry
        self._reader = reader

    async def state(self, job_id: str) -> ResultState:
        """Return the committed state of one job."""
        if self._registry.by_job(job_id) is None:
            return ResultState.NOT_FOUND
        if await self._marker(job_id) is None:
            return ResultState.IN_PROGRESS
        return ResultState.COMPLETED

    async def manifest(self, job_id: str) -> ResultManifest | None:
        """Return the committed manifest, or None when nothing is committed."""
        content = await self._marker(job_id)
        if content is None:
            return None
        try:
            manifest = ResultManifest.model_validate_json(content)
        except ValidationError as error:
            raise AppError(
                "result_marker_invalid",
                "A committed result marker does not satisfy the result contract.",
                context={"job_id": job_id},
                cause=error,
            ) from error
        if manifest.job_id != job_id:
            raise AppError(
                "result_marker_mismatched",
                "A result marker names a different job than the key it sits under.",
                context={"job_id": job_id},
            )
        return manifest

    async def _marker(self, job_id: str) -> bytes | None:
        """Read the result marker for a registered job."""
        record = self._registry.by_job(job_id)
        if record is None:
            return None
        return await self._reader.get(record.commit_key)
