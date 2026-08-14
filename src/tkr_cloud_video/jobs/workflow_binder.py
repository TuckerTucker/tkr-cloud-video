"""Allowlisted request binding into a digest-pinned ComfyUI API workflow."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.jobs.contracts import RequestBase
from tkr_cloud_video.security.validation import Sha256Digest


class WorkflowBindingError(AppError):
    """Pinned workflow identity or binding map is invalid."""


@dataclass(frozen=True, slots=True)
class ParameterBinding:
    """One approved request field to exact node input mapping."""

    request_field: str
    node_id: str
    input_name: str


class WorkflowBinder:
    """Mutates only declared node inputs after verifying canonical workflow bytes."""

    def bind(
        self,
        workflow_bytes: bytes,
        expected_digest: Sha256Digest,
        bindings: tuple[ParameterBinding, ...],
        request: RequestBase,
        runtime_values: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Return a bound copy or fail before prompt submission."""
        if hashlib.sha256(workflow_bytes).hexdigest() != str(expected_digest):
            raise WorkflowBindingError(
                "workflow_digest_mismatch", "Workflow identity differs."
            )
        workflow: dict[str, Any] = json.loads(workflow_bytes)
        bound = copy.deepcopy(workflow)
        values = request.model_dump(mode="json")
        values.update(runtime_values or {})
        if values.get("prompt") is None:
            raise WorkflowBindingError(
                "unrendered_prompt_submitted",
                "A structured prompt must be rendered before binding.",
                context={"field": "prompt"},
            )
        values.pop("structured_prompt", None)
        for binding in bindings:
            node = bound.get(binding.node_id)
            if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
                raise WorkflowBindingError(
                    "binding_node_missing", "Approved workflow node is missing."
                )
            inputs: dict[str, Any] = node["inputs"]
            if binding.input_name not in inputs or binding.request_field not in values:
                raise WorkflowBindingError(
                    "binding_input_missing", "Approved workflow input is missing."
                )
            inputs[binding.input_name] = values[binding.request_field]
        return bound
