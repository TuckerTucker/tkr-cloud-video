"""Per-process environment construction with explicit secret allowlists."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol

from tkr_cloud_video.core.context import validate_identifier
from tkr_cloud_video.security.credentials import SecretExposureError


class ProcessRole(StrEnum):
    """Worker subprocess trust boundaries."""

    HYDRATOR = "hydrator"
    INPUT_READER = "input-reader"
    COMFYUI = "comfyui"
    UPLOADER = "uploader"
    DELIVERY = "delivery"


SECRET_VARIABLES: Final[dict[ProcessRole, frozenset[str]]] = {
    ProcessRole.HYDRATOR: frozenset({"B2_MODEL_KEY_ID", "B2_MODEL_APPLICATION_KEY"}),
    ProcessRole.INPUT_READER: frozenset(
        {"B2_INPUT_KEY_ID", "B2_INPUT_APPLICATION_KEY"}
    ),
    ProcessRole.COMFYUI: frozenset(),
    ProcessRole.UPLOADER: frozenset({"B2_OUTPUT_KEY_ID", "B2_OUTPUT_APPLICATION_KEY"}),
    ProcessRole.DELIVERY: frozenset(
        {"B2_DELIVERY_KEY_ID", "B2_DELIVERY_APPLICATION_KEY"}
    ),
}
FORBIDDEN_PUBLIC_PARTS: Final[tuple[str, ...]] = (
    "AUTHORIZATION",
    "CREDENTIAL",
    "PASSWORD",
    "SECRET",
    "SIGNED_URL",
    "TOKEN",
)


@dataclass(frozen=True, slots=True)
class SecretReference:
    """Maps one process variable to a RunPod secret name without its value."""

    variable: str
    secret_name: str

    def __post_init__(self) -> None:
        """Validate names as bounded environment-compatible identifiers."""
        if not self.variable or not self.variable.replace("_", "").isalnum():
            raise SecretExposureError(
                "secret_reference_invalid",
                "Secret environment variable name is invalid.",
                context={"field": "variable"},
            )
        validate_identifier(self.secret_name, "resource_id")


class SecretResolver(Protocol):
    """Platform adapter for resolving secret references in process memory."""

    def resolve(self, reference: SecretReference) -> str:
        """Resolve one secret value without persisting it."""
        ...


class ProcessEnvironmentBuilder:
    """Builds a fresh environment instead of inheriting ambient process state."""

    def __init__(self, resolver: SecretResolver) -> None:
        """Initialize with an explicit platform secret resolver."""
        self._resolver = resolver

    def build(
        self,
        role: ProcessRole,
        public_values: Mapping[str, str],
        secret_references: tuple[SecretReference, ...] = (),
    ) -> dict[str, str]:
        """Return only public values and role-allowlisted resolved secrets."""
        for name in public_values:
            upper_name = name.upper()
            if any(part in upper_name for part in FORBIDDEN_PUBLIC_PARTS):
                raise SecretExposureError(
                    "secret_exposure_detected",
                    "A secret-bearing value was supplied as public configuration.",
                    context={"field": "public_values"},
                )
        allowed = SECRET_VARIABLES[role]
        supplied = {reference.variable for reference in secret_references}
        if not supplied.issubset(allowed):
            raise SecretExposureError(
                "secret_exposure_detected",
                "A process received a secret outside its role allowlist.",
                context={"resource_id": role.value},
            )
        environment = dict(public_values)
        for reference in secret_references:
            environment[reference.variable] = self._resolver.resolve(reference)
        return environment


def runpod_template_reference(secret_name: str) -> str:
    """Render the documented RunPod template reference syntax."""
    validate_identifier(secret_name, "resource_id")
    return "{{ RUNPOD_SECRET_" + secret_name + " }}"
