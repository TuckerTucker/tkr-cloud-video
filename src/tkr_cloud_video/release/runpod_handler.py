"""Thin contract-compatible RunPod Serverless handler."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from pydantic import ValidationError

from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.jobs.contracts import GenerationRequest, parse_generation_request


class JobApplication(Protocol):
    """Shared intake/execution/delivery application boundary."""

    async def submit(self, request: GenerationRequest) -> tuple[str, str | None]:
        """Return job identity and optional committed result reference."""
        ...


@dataclass(frozen=True, slots=True)
class HandlerResponse:
    """Stable Serverless response containing references, never media bytes."""

    ok: bool
    job_id: str | None = None
    result_reference: str | None = None
    error_code: str | None = None
    retryable: bool = False
    # The error's allowlisted context, which names what failed: the workflow
    # node, the field, the operation. Without it a caller sees only the code
    # and has to read worker output to learn which node stopped the run, which
    # a caller holding nothing but the response cannot do.
    error_context: Mapping[str, str | int | float | bool | None] | None = None


class RunPodHandler:
    """Translates RunPod event envelopes into the shared request contract."""

    def __init__(self, application: JobApplication) -> None:
        """Initialize with the shared application use case."""
        self._application = application

    def validate(self, event: object) -> GenerationRequest | HandlerResponse:
        """Return a typed request or a stable validation response."""
        if not isinstance(event, dict) or "input" not in event:
            return HandlerResponse(False, error_code="invalid_envelope")
        try:
            return parse_generation_request(event["input"])
        except ValidationError:
            return HandlerResponse(False, error_code="invalid_request")

    async def handle_request(self, request: GenerationRequest) -> HandlerResponse:
        """Submit one validated request and return only safe references."""
        try:
            job_id, reference = await self._application.submit(request)
        except AppError as error:
            return HandlerResponse(
                False,
                error_code=error.code,
                retryable=error.retryable,
                error_context=dict(error.context) or None,
            )
        return HandlerResponse(True, job_id, reference)

    async def handle(self, event: object) -> HandlerResponse:
        """Validate before application work and return only safe references."""
        validated = self.validate(event)
        if isinstance(validated, HandlerResponse):
            return validated
        return await self.handle_request(validated)
