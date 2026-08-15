"""IoC composition for verified commit, private access, and retention.

Retention is composed separately from delivery on purpose. Expiry and erasure
need a credential holding ``deleteFiles``, which is control-plane only, so the
retention store is optional here: a worker or delivery process composes without
it and is then structurally incapable of deleting, rather than merely trusted
not to.
"""

from __future__ import annotations

from dataclasses import dataclass

from tkr_cloud_video.core.clock import Clock
from tkr_cloud_video.delivery.access_policy import ResultAccessPolicy, ResultAuthorizer
from tkr_cloud_video.delivery.api import PrivateResultService, ResultRepository
from tkr_cloud_video.delivery.commit import ResultCommitter
from tkr_cloud_video.delivery.lifecycle import LifecyclePolicy
from tkr_cloud_video.delivery.lifecycle_rules import (
    BucketLifecycleRule,
    LifecycleRuleSet,
)
from tkr_cloud_video.delivery.reconciler import RetentionReconciler, RetentionStore
from tkr_cloud_video.delivery.signed_links import LinkSigner, SignedLinkService
from tkr_cloud_video.delivery.subject_requests import SubjectRequestService
from tkr_cloud_video.delivery.uploader import ResultStore, VerifiedUploader


@dataclass(frozen=True, slots=True)
class DeliveryDependencies:
    """External delivery storage, authorization, signing, and clock ports."""

    store: ResultStore
    authorizer: ResultAuthorizer
    repository: ResultRepository
    signer: LinkSigner
    clock: Clock
    signed_link_ttl_seconds: int
    lifecycle_policy: LifecyclePolicy
    # Absent for every process that must not delete. Its absence is what makes
    # the retention services absent too.
    retention_store: RetentionStore | None = None


@dataclass(frozen=True, slots=True)
class DeliveryServices:
    """Composed result commit, private delivery, and retention services."""

    uploader: VerifiedUploader
    committer: ResultCommitter
    private_results: PrivateResultService
    lifecycle_policy: LifecyclePolicy
    declared_rules: tuple[BucketLifecycleRule, ...]
    reconciler: RetentionReconciler | None
    subject_requests: SubjectRequestService | None

    @property
    def can_delete(self) -> bool:
        """Return whether this composition carries expiry and erasure."""
        return self.reconciler is not None


def compose_durable_delivery(
    dependencies: DeliveryDependencies,
) -> DeliveryServices:
    """Compose delivery behavior from least-privilege adapters."""
    access = ResultAccessPolicy(dependencies.authorizer)
    links = SignedLinkService(
        dependencies.signer, dependencies.clock, dependencies.signed_link_ttl_seconds
    )
    retention_store = dependencies.retention_store
    reconciler = (
        RetentionReconciler(retention_store) if retention_store is not None else None
    )
    subject_requests = (
        SubjectRequestService(access, retention_store, dependencies.lifecycle_policy)
        if retention_store is not None
        else None
    )
    return DeliveryServices(
        VerifiedUploader(dependencies.store),
        ResultCommitter(dependencies.store),
        PrivateResultService(access, dependencies.repository, links),
        dependencies.lifecycle_policy,
        LifecycleRuleSet().project(dependencies.lifecycle_policy),
        reconciler,
        subject_requests,
    )
