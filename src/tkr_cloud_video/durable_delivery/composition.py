"""IoC composition for verified commit, private access, and retention."""

from __future__ import annotations

from dataclasses import dataclass

from tkr_cloud_video.core.clock import Clock
from tkr_cloud_video.delivery.access_policy import ResultAccessPolicy, ResultAuthorizer
from tkr_cloud_video.delivery.api import PrivateResultService, ResultRepository
from tkr_cloud_video.delivery.commit import ResultCommitter
from tkr_cloud_video.delivery.lifecycle import LifecyclePolicy
from tkr_cloud_video.delivery.signed_links import LinkSigner, SignedLinkService
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


@dataclass(frozen=True, slots=True)
class DeliveryServices:
    """Composed result commit and private delivery services."""

    uploader: VerifiedUploader
    committer: ResultCommitter
    private_results: PrivateResultService
    lifecycle_policy: LifecyclePolicy


def compose_durable_delivery(
    dependencies: DeliveryDependencies,
) -> DeliveryServices:
    """Compose delivery behavior from least-privilege adapters."""
    access = ResultAccessPolicy(dependencies.authorizer)
    links = SignedLinkService(
        dependencies.signer, dependencies.clock, dependencies.signed_link_ttl_seconds
    )
    return DeliveryServices(
        VerifiedUploader(dependencies.store),
        ResultCommitter(dependencies.store),
        PrivateResultService(access, dependencies.repository, links),
        dependencies.lifecycle_policy,
    )
