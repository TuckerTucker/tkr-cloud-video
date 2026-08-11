"""Provider-neutral least-privilege credential boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol

from tkr_cloud_video.core.context import validate_identifier
from tkr_cloud_video.core.errors import AppError


class CredentialRole(StrEnum):
    """Distinct runtime storage trust boundaries."""

    MODEL_READER = "model-reader"
    INPUT_READER = "input-reader"
    OUTPUT_WRITER = "output-writer"
    DELIVERY_READER = "delivery-reader"


class StorageCapability(StrEnum):
    """Backblaze-compatible file capability vocabulary."""

    LIST_FILES = "listFiles"
    READ_FILES = "readFiles"
    SHARE_FILES = "shareFiles"
    WRITE_FILES = "writeFiles"
    DELETE_FILES = "deleteFiles"


ROLE_CAPABILITIES: Final[dict[CredentialRole, frozenset[StorageCapability]]] = {
    CredentialRole.MODEL_READER: frozenset(
        {StorageCapability.LIST_FILES, StorageCapability.READ_FILES}
    ),
    CredentialRole.INPUT_READER: frozenset(
        {StorageCapability.LIST_FILES, StorageCapability.READ_FILES}
    ),
    CredentialRole.OUTPUT_WRITER: frozenset(
        {
            StorageCapability.LIST_FILES,
            StorageCapability.READ_FILES,
            StorageCapability.WRITE_FILES,
        }
    ),
    CredentialRole.DELIVERY_READER: frozenset(
        {
            StorageCapability.LIST_FILES,
            StorageCapability.READ_FILES,
            StorageCapability.SHARE_FILES,
        }
    ),
}


class CredentialScopeError(AppError):
    """A credential requests a broader boundary than its role permits."""


class ProviderCapabilityMismatchError(AppError):
    """Provider probe behavior differs from declared credential policy."""


class SecretExposureError(AppError):
    """Secret-bearing data crossed a forbidden boundary."""


@dataclass(frozen=True, slots=True)
class CredentialScope:
    """One bucket, one prefix, one role, and its exact capabilities."""

    role: CredentialRole
    bucket_id: str
    prefix: str
    capabilities: frozenset[StorageCapability]

    def __post_init__(self) -> None:
        """Validate the single-prefix least-privilege invariant."""
        validate_identifier(self.bucket_id, "resource_id")
        if (
            not self.prefix
            or self.prefix.startswith("/")
            or not self.prefix.endswith("/")
            or "//" in self.prefix
            or any(
                part in {"", ".", ".."}
                for part in self.prefix.removesuffix("/").split("/")
            )
            or any(ord(character) < 32 for character in self.prefix)
            or "\\" in self.prefix
        ):
            raise CredentialScopeError(
                "credential_scope_invalid",
                "Credential prefix must be one normalized object namespace.",
                context={"field": "prefix"},
            )
        expected = ROLE_CAPABILITIES[self.role]
        if self.capabilities != expected:
            raise CredentialScopeError(
                "credential_scope_invalid",
                "Credential capabilities do not match the process role.",
                context={"resource_id": self.role.value},
            )

    @classmethod
    def for_role(
        cls, role: CredentialRole, bucket_id: str, prefix: str
    ) -> CredentialScope:
        """Construct the exact approved scope for a runtime role."""
        return cls(role, bucket_id, prefix, ROLE_CAPABILITIES[role])

    def permits(
        self, capability: StorageCapability, *, bucket_id: str, object_key: str
    ) -> bool:
        """Evaluate a provider operation against bucket, prefix, and capability."""
        return (
            bucket_id == self.bucket_id
            and object_key.startswith(self.prefix)
            and capability in self.capabilities
        )


@dataclass(frozen=True, slots=True)
class ProbeReceipt:
    """Safe evidence for one allowed or forbidden capability probe."""

    role: CredentialRole
    capability: StorageCapability
    expected_allowed: bool
    observed_allowed: bool
    passed: bool


class CredentialProbe(Protocol):
    """Provider adapter used to prove credential enforcement."""

    async def probe(
        self,
        secret_reference: str,
        scope: CredentialScope,
        capability: StorageCapability,
        *,
        use_forbidden_prefix: bool,
    ) -> bool:
        """Return whether the provider authorized the synthetic operation."""
        ...


class CredentialPolicy:
    """Evaluates exact role scopes and expected provider authorization."""

    def scope_for(
        self, role: CredentialRole, bucket_id: str, prefix: str
    ) -> CredentialScope:
        """Return the canonical policy scope for a runtime role."""
        return CredentialScope.for_role(role, bucket_id, prefix)

    def expected_probe_matrix(
        self, scope: CredentialScope
    ) -> tuple[tuple[StorageCapability, bool, bool], ...]:
        """Return capability, forbidden-prefix flag, and expected outcome rows."""
        rows: list[tuple[StorageCapability, bool, bool]] = []
        for capability in StorageCapability:
            rows.append((capability, False, capability in scope.capabilities))
        for capability in scope.capabilities:
            rows.append((capability, True, False))
        return tuple(rows)
