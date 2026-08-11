"""Principal-scoped idempotent job identity allocation."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from enum import StrEnum

from tkr_cloud_video.core.context import validate_identifier
from tkr_cloud_video.core.errors import AppError


class JobState(StrEnum):
    """Durable-enough intake and execution states."""

    ACCEPTED = "accepted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class JobIdentity:
    """Server-owned job and attempt identities bound to a request hash."""

    job_id: str
    attempt_id: str
    principal_id: str
    idempotency_key: str
    request_hash: str
    state: JobState


class IdempotencyConflictError(AppError):
    """An idempotency key was reused with a different payload."""


class IdempotencyRegistry:
    """Concurrent in-memory registry; production stores implement the same policy."""

    def __init__(self) -> None:
        """Initialize a lock-protected principal/key index."""
        self._records: dict[tuple[str, str], JobIdentity] = {}
        self._lock = asyncio.Lock()

    async def allocate(
        self, principal_id: str, idempotency_key: str, request_hash: str
    ) -> JobIdentity:
        """Converge duplicates or reject a conflicting payload atomically."""
        validate_identifier(principal_id, "resource_id")
        validate_identifier(idempotency_key, "resource_id")
        async with self._lock:
            key = (principal_id, idempotency_key)
            current = self._records.get(key)
            if current is not None:
                if current.request_hash != request_hash:
                    raise IdempotencyConflictError(
                        "idempotency_conflict",
                        "Idempotency key is bound to a different request.",
                        context={"resource_id": idempotency_key},
                    )
                return current
            record = JobIdentity(
                job_id=f"job-{uuid.uuid4().hex}",
                attempt_id=f"attempt-{uuid.uuid4().hex}",
                principal_id=principal_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                state=JobState.ACCEPTED,
            )
            self._records[key] = record
            return record
