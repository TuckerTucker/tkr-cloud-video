"""Thin contract-compatible RunPod Serverless handler."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from pydantic import ValidationError

from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.jobs.contracts import GenerationRequest, parse_generation_request
from tkr_cloud_video.prompt_authoring.composition import (
    PromptServices,
    resolve_request_prompt,
)
from tkr_cloud_video.prompting.errors import PromptValidationError


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
    # Every field-scoped prompt defect, not just the first. `error_context`
    # holds one flat mapping, so on its own it would narrow a multi-defect
    # rejection to one rule and cost a round trip per remaining defect - which
    # is exactly the cost the defect list exists to remove.
    defects: tuple[Mapping[str, str], ...] | None = None


class RunPodHandler:
    """Translates RunPod event envelopes into the shared request contract."""

    def __init__(self, application: JobApplication, prompts: PromptServices) -> None:
        """Initialize with the shared application use case and prompt boundary.

        Args:
            application: The shared intake/execution/delivery boundary.
            prompts: The composed prompt boundary. Required rather than
                optional: a handler that cannot compose a prompt cannot tell an
                unrenderable request from a renderable one, and defaulting it
                away would fail open into exactly the deferred rejection this
                parameter exists to prevent.

        """
        self._application = application
        self._prompts = prompts

    def validate(self, event: object) -> GenerationRequest | HandlerResponse:
        """Return a typed request or a stable validation response.

        The request contract types ``structured_prompt`` as an opaque mapping,
        so parsing alone cannot tell whether the prompt renders. Composing it
        here rather than at execution is what keeps a closed-vocabulary defect
        from costing a worker cold start before it is reported: the deployment
        starts nothing until this returns a request.

        The rendered text is discarded. It is derived again where it is bound
        to the workflow, from the same function and the same request, so the
        preflight cannot accept text the binder would not have produced.
        """
        if not isinstance(event, dict) or "input" not in event:
            return HandlerResponse(False, error_code="invalid_envelope")
        try:
            request = parse_generation_request(event["input"])
        except ValidationError:
            return HandlerResponse(False, error_code="invalid_request")
        try:
            resolve_request_prompt(request, self._prompts)
        except PromptValidationError as error:
            return _rejected(
                error, tuple(defect.as_context() for defect in error.defects)
            )
        except AppError as error:
            return _rejected(error, ())
        return request

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


def _rejected(
    error: AppError, defects: tuple[Mapping[str, str], ...]
) -> HandlerResponse:
    """Return a prompt rejection carrying the error's own code and context.

    The prompt errors already name the field and the rule and never carry the
    offending text, so they are reported as themselves rather than collapsed
    into ``invalid_request``: a caller told only that its request was invalid
    has to guess which of its slots broke a closed vocabulary.
    """
    return HandlerResponse(
        False,
        error_code=error.code,
        retryable=error.retryable,
        error_context=dict(error.context) or None,
        defects=defects or None,
    )
