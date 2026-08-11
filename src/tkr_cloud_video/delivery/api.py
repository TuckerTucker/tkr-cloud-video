"""Authorized private result lookup and download service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from tkr_cloud_video.delivery.access_policy import ResultAccessPolicy, ResultState
from tkr_cloud_video.delivery.contracts import ResultManifest, ResultRole
from tkr_cloud_video.delivery.signed_links import DownloadLink, SignedLinkService


class ResultRepository(Protocol):
    """Job-state and committed-manifest lookup port."""

    async def state(self, job_id: str) -> ResultState:
        """Return stable job result state."""
        ...

    async def manifest(self, job_id: str) -> ResultManifest | None:
        """Return the committed manifest only when visible."""
        ...


@dataclass(frozen=True, slots=True)
class ResultResponse:
    """Sanitized lookup response without provider credentials or URLs."""

    state: ResultState
    manifest: ResultManifest | None = None


class PrivateResultService:
    """Authorizes before exposing state, metadata, or signed delivery."""

    def __init__(
        self,
        access: ResultAccessPolicy,
        repository: ResultRepository,
        links: SignedLinkService,
    ) -> None:
        """Initialize with separate authorization, state, and signing ports."""
        self._access, self._repository, self._links = access, repository, links

    async def lookup(self, principal_id: str, job_id: str) -> ResultResponse:
        """Return non-enumerating state and committed metadata."""
        if not await self._access.authorize(principal_id, job_id):
            return ResultResponse(ResultState.NOT_FOUND)
        state = await self._repository.state(job_id)
        manifest = (
            await self._repository.manifest(job_id)
            if state is ResultState.COMPLETED
            else None
        )
        return ResultResponse(state, manifest)

    async def download(
        self, principal_id: str, job_id: str, ttl_seconds: int
    ) -> DownloadLink | None:
        """Reauthorize and issue a link only for a committed video."""
        response = await self.lookup(principal_id, job_id)
        if response.state is not ResultState.COMPLETED or response.manifest is None:
            return None
        video = next(
            artifact
            for artifact in response.manifest.artifacts
            if artifact.role is ResultRole.VIDEO
        )
        return await self._links.issue(video.remote_key, ttl_seconds)
