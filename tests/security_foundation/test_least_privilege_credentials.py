"""Credential boundary, provisioning, rotation, and process isolation tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from tests.conftest import FakeClock
from tkr_cloud_video.core.context import OperationContext
from tkr_cloud_video.security.b2_keys import (
    B2KeyAdministrator,
    B2KeyProvisioningService,
    B2KeyRequest,
    ProvisionedKey,
)
from tkr_cloud_video.security.credentials import (
    ROLE_CAPABILITIES,
    CredentialPolicy,
    CredentialProbe,
    CredentialRole,
    CredentialScope,
    CredentialScopeError,
    ProviderCapabilityMismatchError,
    SecretExposureError,
    StorageCapability,
)
from tkr_cloud_video.security.process_secrets import (
    ProcessEnvironmentBuilder,
    ProcessRole,
    SecretReference,
    SecretResolver,
    runpod_template_reference,
)
from tkr_cloud_video.security_foundation.composition import (
    SecurityDependencies,
    compose_security_foundation,
)


@dataclass
class FakeAdministrator(B2KeyAdministrator):
    """Records secret-free provider requests and revocations."""

    created: list[B2KeyRequest] = field(default_factory=list)
    revoked: list[str] = field(default_factory=list)

    async def create_key(self, request: B2KeyRequest) -> ProvisionedKey:
        """Return opaque references without key material."""
        self.created.append(request)
        sequence = len(self.created)
        return ProvisionedKey(f"key-{sequence}", f"runpod-secret-{sequence}")

    async def revoke_key(self, provider_key_id: str) -> None:
        """Record a revocation."""
        self.revoked.append(provider_key_id)


@dataclass
class PolicyProbe(CredentialProbe):
    """Mimics provider enforcement, optionally allowing one forbidden probe."""

    allow_forbidden: bool = False

    async def probe(
        self,
        secret_reference: str,
        scope: CredentialScope,
        capability: StorageCapability,
        *,
        use_forbidden_prefix: bool,
    ) -> bool:
        """Evaluate through policy unless failure injection broadens access."""
        assert secret_reference.startswith("runpod-secret-")
        if use_forbidden_prefix and self.allow_forbidden:
            return True
        object_key = (
            "forbidden/probe" if use_forbidden_prefix else scope.prefix + "probe"
        )
        return scope.permits(
            capability, bucket_id=scope.bucket_id, object_key=object_key
        )


class MappingResolver(SecretResolver):
    """Synthetic in-memory secret resolver."""

    def resolve(self, reference: SecretReference) -> str:
        """Return deterministic synthetic secret content."""
        return f"synthetic-{reference.secret_name}"


def operation_context() -> OperationContext:
    """Build a valid deterministic security operation context."""
    return OperationContext(
        release_id="release-1",
        worker_id="worker-1",
        correlation_id="correlation-1",
        deadline=datetime(2026, 1, 2, tzinfo=UTC),
    )


@pytest.mark.parametrize("role", list(CredentialRole))
def test_each_role_has_exact_capabilities_and_single_prefix(
    role: CredentialRole,
) -> None:
    """Role constructors cannot accidentally request broader capabilities."""
    scope = CredentialScope.for_role(role, "bucket-1", f"{role.value}/")

    assert scope.capabilities == ROLE_CAPABILITIES[role]
    assert scope.permits(
        next(iter(scope.capabilities)),
        bucket_id="bucket-1",
        object_key=f"{role.value}/object",
    )
    assert not scope.permits(
        next(iter(scope.capabilities)),
        bucket_id="bucket-2",
        object_key=f"{role.value}/object",
    )


@pytest.mark.parametrize("prefix", ["", "/models/", "models", "../models/", "a//b/"])
def test_scope_rejects_ambiguous_prefix(prefix: str) -> None:
    """Every credential requires one normalized folder-like prefix."""
    with pytest.raises(CredentialScopeError):
        CredentialScope.for_role(CredentialRole.MODEL_READER, "bucket-1", prefix)


def test_scope_rejects_noncanonical_capabilities() -> None:
    """Adding or removing a role capability fails at model construction."""
    with pytest.raises(CredentialScopeError):
        CredentialScope(
            CredentialRole.MODEL_READER,
            "bucket-1",
            "models/",
            frozenset({StorageCapability.READ_FILES}),
        )


@pytest.mark.asyncio
async def test_provisioning_probes_allowed_and_forbidden_operations() -> None:
    """A key activates only after its complete provider matrix passes."""
    administrator = FakeAdministrator()
    service = B2KeyProvisioningService(CredentialPolicy(), administrator, PolicyProbe())

    result = await service.provision(
        CredentialRole.MODEL_READER, "bucket-1", "models/", operation_context()
    )

    assert administrator.revoked == []
    assert all(receipt.passed for receipt in result.receipts)
    assert any(not receipt.expected_allowed for receipt in result.receipts)
    assert administrator.created[0].capabilities == ("listFiles", "readFiles")


@pytest.mark.asyncio
async def test_broader_provider_behavior_revokes_candidate() -> None:
    """A forbidden-prefix success fails closed and revokes the new key."""
    administrator = FakeAdministrator()
    service = B2KeyProvisioningService(
        CredentialPolicy(), administrator, PolicyProbe(allow_forbidden=True)
    )

    with pytest.raises(ProviderCapabilityMismatchError):
        await service.provision(
            CredentialRole.INPUT_READER,
            "bucket-1",
            "inputs/",
            operation_context(),
        )
    assert administrator.revoked == ["key-1"]


@pytest.mark.asyncio
async def test_rotation_verifies_replacement_before_revoking_old_key() -> None:
    """Rotation changes only secret references and revokes superseded identity."""
    administrator = FakeAdministrator()
    service = B2KeyProvisioningService(CredentialPolicy(), administrator, PolicyProbe())
    original = await service.provision(
        CredentialRole.OUTPUT_WRITER,
        "bucket-1",
        "outputs/",
        operation_context(),
    )

    replacement = await service.rotate(original, operation_context())

    assert replacement.scope == original.scope
    assert replacement.secret_reference != original.secret_reference
    assert administrator.revoked == [original.provider_key_id]


def test_comfyui_environment_contains_no_storage_secret() -> None:
    """ComfyUI receives only explicit non-secret model/runtime configuration."""
    builder = ProcessEnvironmentBuilder(MappingResolver())
    environment = builder.build(ProcessRole.COMFYUI, {"MODEL_ROOT": "/models"})

    assert environment == {"MODEL_ROOT": "/models"}


def test_role_environment_resolves_only_allowlisted_secrets() -> None:
    """Uploader can resolve its output credential without ambient inheritance."""
    builder = ProcessEnvironmentBuilder(MappingResolver())
    references = (
        SecretReference("B2_OUTPUT_KEY_ID", "output-key-id"),
        SecretReference("B2_OUTPUT_APPLICATION_KEY", "output-application-key"),
    )

    environment = builder.build(ProcessRole.UPLOADER, {"LOG_LEVEL": "INFO"}, references)

    assert set(environment) == {
        "LOG_LEVEL",
        "B2_OUTPUT_KEY_ID",
        "B2_OUTPUT_APPLICATION_KEY",
    }
    assert runpod_template_reference("output-key-id") == (
        "{{ RUNPOD_SECRET_output-key-id }}"
    )


def test_process_environment_rejects_cross_role_or_public_secret() -> None:
    """Secrets cannot be smuggled through public values or into ComfyUI."""
    builder = ProcessEnvironmentBuilder(MappingResolver())
    reference = SecretReference("B2_OUTPUT_KEY_ID", "output-key-id")

    with pytest.raises(SecretExposureError):
        builder.build(ProcessRole.COMFYUI, {}, (reference,))
    with pytest.raises(SecretExposureError):
        builder.build(ProcessRole.COMFYUI, {"API_TOKEN": "synthetic-marker"})


def test_composition_preserves_injected_adapters() -> None:
    """The public capability boundary constructs all services without globals."""
    administrator = FakeAdministrator()
    probe = PolicyProbe()
    resolver = MappingResolver()

    services = compose_security_foundation(
        SecurityDependencies(FakeClock(), administrator, probe, resolver)
    )

    assert services.key_provisioning is not None
    assert services.process_environments is not None
    assert services.release_gate is not None
