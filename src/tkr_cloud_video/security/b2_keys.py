"""Backblaze B2 key provisioning and rotation service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from tkr_cloud_video.core.context import OperationContext
from tkr_cloud_video.security.credentials import (
    CredentialPolicy,
    CredentialProbe,
    CredentialRole,
    CredentialScope,
    ProbeReceipt,
    ProviderCapabilityMismatchError,
)


@dataclass(frozen=True, slots=True)
class B2KeyRequest:
    """Secret-free administrative request for one standard B2 application key."""

    key_name: str
    bucket_id: str
    name_prefix: str
    capabilities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProvisionedKey:
    """Opaque provider identity and secret-store reference, never key material."""

    provider_key_id: str
    secret_reference: str


class B2KeyAdministrator(Protocol):
    """Administrative provider port that must never be available on workers."""

    async def create_key(self, request: B2KeyRequest) -> ProvisionedKey:
        """Create a standard prefix-restricted key and store its secret."""
        ...

    async def revoke_key(self, provider_key_id: str) -> None:
        """Revoke a provider key by opaque identifier."""
        ...


@dataclass(frozen=True, slots=True)
class ProvisionedCredential:
    """Activated credential metadata and its safe verification evidence."""

    scope: CredentialScope
    provider_key_id: str
    secret_reference: str
    receipts: tuple[ProbeReceipt, ...]


class B2KeyProvisioningService:
    """Provisions and activates keys only after the full probe matrix passes."""

    def __init__(
        self,
        policy: CredentialPolicy,
        administrator: B2KeyAdministrator,
        probe: CredentialProbe,
    ) -> None:
        """Initialize with separate policy, administrative, and probe ports."""
        self._policy = policy
        self._administrator = administrator
        self._probe = probe

    async def provision(
        self,
        role: CredentialRole,
        bucket_id: str,
        prefix: str,
        context: OperationContext,
    ) -> ProvisionedCredential:
        """Provision a role key and fail closed if provider behavior is broader."""
        scope = self._policy.scope_for(role, bucket_id, prefix)
        request = B2KeyRequest(
            key_name=f"{context.release_id}-{role.value}",
            bucket_id=scope.bucket_id,
            name_prefix=scope.prefix,
            capabilities=tuple(sorted(item.value for item in scope.capabilities)),
        )
        provisioned = await self._administrator.create_key(request)
        receipts: list[ProbeReceipt] = []
        try:
            for (
                capability,
                forbidden_prefix,
                expected,
            ) in self._policy.expected_probe_matrix(scope):
                observed = await self._probe.probe(
                    provisioned.secret_reference,
                    scope,
                    capability,
                    use_forbidden_prefix=forbidden_prefix,
                )
                receipt = ProbeReceipt(
                    role=role,
                    capability=capability,
                    expected_allowed=expected,
                    observed_allowed=observed,
                    passed=observed is expected,
                )
                receipts.append(receipt)
                if not receipt.passed:
                    raise ProviderCapabilityMismatchError(
                        "provider_capability_mismatch",
                        "Credential probe did not enforce its declared boundary.",
                        context={"resource_id": role.value},
                    )
        except Exception:
            await self._administrator.revoke_key(provisioned.provider_key_id)
            raise
        return ProvisionedCredential(
            scope=scope,
            provider_key_id=provisioned.provider_key_id,
            secret_reference=provisioned.secret_reference,
            receipts=tuple(receipts),
        )

    async def rotate(
        self,
        current: ProvisionedCredential,
        context: OperationContext,
    ) -> ProvisionedCredential:
        """Activate a verified replacement before revoking the prior key."""
        replacement = await self.provision(
            current.scope.role,
            current.scope.bucket_id,
            current.scope.prefix,
            context,
        )
        await self._administrator.revoke_key(current.provider_key_id)
        return replacement
