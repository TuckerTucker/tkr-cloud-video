"""IoC composition root for the security foundation capability."""

from __future__ import annotations

from dataclasses import dataclass

from tkr_cloud_video.core.clock import Clock
from tkr_cloud_video.security.b2_keys import (
    B2KeyAdministrator,
    B2KeyProvisioningService,
)
from tkr_cloud_video.security.credentials import CredentialPolicy, CredentialProbe
from tkr_cloud_video.security.process_secrets import (
    ProcessEnvironmentBuilder,
    SecretResolver,
)
from tkr_cloud_video.security.release_gate import ReleaseAssuranceGate


@dataclass(frozen=True, slots=True)
class SecurityDependencies:
    """All side-effecting ports required by security services."""

    clock: Clock
    key_administrator: B2KeyAdministrator
    credential_probe: CredentialProbe
    secret_resolver: SecretResolver


@dataclass(frozen=True, slots=True)
class SecurityServices:
    """Fully composed security services exposed to later capabilities."""

    credential_policy: CredentialPolicy
    key_provisioning: B2KeyProvisioningService
    process_environments: ProcessEnvironmentBuilder
    release_gate: ReleaseAssuranceGate


def compose_security_foundation(
    dependencies: SecurityDependencies,
) -> SecurityServices:
    """Compose security policy and services from injected platform adapters."""
    policy = CredentialPolicy()
    return SecurityServices(
        credential_policy=policy,
        key_provisioning=B2KeyProvisioningService(
            policy,
            dependencies.key_administrator,
            dependencies.credential_probe,
        ),
        process_environments=ProcessEnvironmentBuilder(dependencies.secret_resolver),
        release_gate=ReleaseAssuranceGate(dependencies.clock),
    )
