"""Non-enumerating private result authorization policy."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol


class ResultState(StrEnum):
    """Stable externally visible result states."""

    NOT_FOUND = "not-found"
    IN_PROGRESS = "in-progress"
    FAILED = "failed"
    EXPIRED = "expired"
    COMPLETED = "completed"


class ResultAuthorizer(Protocol):
    """Caller/job authorization port."""

    async def allowed(self, principal_id: str, job_id: str) -> bool:
        """Return whether the principal may observe this job."""
        ...


class ResultAccessPolicy:
    """Maps unauthorized access to the same outcome as absence."""

    def __init__(self, authorizer: ResultAuthorizer) -> None:
        """Initialize with an application-owned authorization adapter."""
        self._authorizer = authorizer

    async def authorize(self, principal_id: str, job_id: str) -> bool:
        """Return false without revealing whether the job exists."""
        return await self._authorizer.allowed(principal_id, job_id)
