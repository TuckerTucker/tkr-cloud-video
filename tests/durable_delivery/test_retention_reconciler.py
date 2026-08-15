"""Reconciler and lifecycle-rule coverage for slice 12.

These assert on storage state after a sweep, never on a predicate returning
true. The predecessor feature's fixtures satisfied their acceptance criterion
while nothing was ever deleted; the difference between that and this is that
every deletion claim below is checked by absence from the store.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tkr_cloud_video.delivery.lifecycle import (
    LifecyclePolicy,
    LifecyclePolicyLoader,
    RetainedClass,
)
from tkr_cloud_video.delivery.lifecycle_rules import (
    BucketLifecycleRule,
    LifecycleRuleSet,
    RetentionError,
    detect_drift,
)
from tkr_cloud_video.delivery.reconciler import (
    RetentionReconciler,
    StoredObject,
    require_delete_capability,
)
from tkr_cloud_video.security.credentials import (
    ROLE_CAPABILITIES,
    CredentialRole,
    StorageCapability,
)
from tkr_cloud_video.security.process_secrets import SECRET_VARIABLES

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
REAPER_VARIABLES = frozenset({"B2_REAPER_KEY_ID", "B2_REAPER_APPLICATION_KEY"})


class MemoryRetentionStore:
    """In-memory delete-capable store recording every removal."""

    def __init__(
        self, objects: dict[str, datetime], *, head_raises: bool = False
    ) -> None:
        """Initialize with keyed upload times and an optional head failure."""
        self.objects = dict(objects)
        self.deleted: list[str] = []
        self._head_raises = head_raises
        self.refuse: set[str] = set()

    async def list_objects(self, prefix: str) -> tuple[StoredObject, ...]:
        """Return every object under a prefix."""
        return tuple(
            StoredObject(key, uploaded)
            for key, uploaded in sorted(self.objects.items())
            if key.startswith(prefix)
        )

    async def head_exists(self, key: str) -> bool:
        """Return whether one object exists, or fail when configured to."""
        if self._head_raises:
            raise RuntimeError("provider unavailable")
        return key in self.objects

    async def delete(self, key: str) -> None:
        """Delete one object unless the provider is set to refuse it."""
        if key in self.refuse:
            raise RuntimeError("provider refused delete")
        self.objects.pop(key, None)
        self.deleted.append(key)


def policy() -> LifecyclePolicy:
    """Load the shipped declared policy."""
    return LifecyclePolicyLoader().load(_ShippedDocument())


class _ShippedDocument:
    """Config source mirroring the shipped declared periods."""

    def read(self) -> dict[str, object]:
        """Return the declared policy document."""
        return {
            "version": "3",
            "rules": {
                "input": 7,
                "failed-attempt": 14,
                "scratch": 2,
                "deliverable": 365,
                "hidden-version": 30,
                "multipart": 2,
                "prompt-evidence": 365,
            },
        }


def attempt(
    job: str, roles: tuple[str, ...], uploaded: datetime
) -> dict[str, datetime]:
    """Build one attempt namespace's objects."""
    return {f"outputs/{job}/attempt-1/{role}": uploaded for role in roles}


COMPLETE = ("video.bin", "workflow.bin", "generation.bin", "result.json")
INCOMPLETE = ("video.bin", "workflow.bin", "generation.bin")


def test_projection_expresses_the_input_period() -> None:
    """S12-T01: inputs get a prefix rule carrying the declared period."""
    rules = LifecycleRuleSet().project(policy())
    inputs = next(rule for rule in rules if rule.file_name_prefix == "inputs/")
    assert inputs.days_from_uploading_to_hiding == 7
    assert inputs.days_from_hiding_to_deleting == 1


def test_projection_covers_hidden_versions_and_multipart() -> None:
    """S12-T01: the version-state classes map to their provider fields."""
    rules = LifecycleRuleSet().project(policy())
    bucket = next(rule for rule in rules if rule.file_name_prefix == "")
    assert bucket.days_from_hiding_to_deleting == 30
    assert bucket.days_from_starting_to_canceling_unfinished_large_files == 2
    assert bucket.days_from_uploading_to_hiding is None


def test_projection_omits_the_classes_a_prefix_cannot_separate() -> None:
    """S12-T02: outputs/ carries no uploading-to-hiding rule.

    A rule there would apply to committed deliverables and failed attempts
    alike, and B2 resolves overlaps by the smallest value — so the shorter
    failed-attempt period would delete deliverables.
    """
    rules = LifecycleRuleSet().project(policy())
    assert not any(
        rule.file_name_prefix.startswith("outputs/")
        and rule.days_from_uploading_to_hiding is not None
        for rule in rules
    )


def test_conflicting_overlap_is_refused() -> None:
    """S12-T02: two periods over overlapping prefixes will not be emitted."""
    ruleset = LifecycleRuleSet()
    conflicting = (
        BucketLifecycleRule("outputs/", days_from_uploading_to_hiding=365),
        BucketLifecycleRule("outputs/job-1/", days_from_uploading_to_hiding=14),
    )
    with pytest.raises(RetentionError) as caught:
        ruleset._reject_conflicting_overlap(conflicting)
    assert caught.value.code == "lifecycle_rule_overlap_conflict"


def test_drift_passes_when_rules_match_the_declaration() -> None:
    """An exact match is the only clean outcome."""
    declared = LifecycleRuleSet().project(policy())
    detect_drift(declared, declared)


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda rules: rules[:1], "lifecycle_rule_missing"),
        (
            lambda rules: (*rules, BucketLifecycleRule("models/", 90)),
            "lifecycle_rule_unexpected",
        ),
        (
            lambda rules: (BucketLifecycleRule("inputs/", 1, 1), *rules[1:]),
            "lifecycle_rule_diverged",
        ),
    ],
)
def test_drift_names_the_class_that_stopped_being_enforced(
    mutate: object, code: str
) -> None:
    """S12-T02: out-of-band change fails loudly and names the prefix."""
    declared = LifecycleRuleSet().project(policy())
    in_force = mutate(declared)  # type: ignore[operator]
    with pytest.raises(RetentionError) as caught:
        detect_drift(declared, in_force)
    assert caught.value.code == code
    assert caught.value.context["rule"]


def test_empty_rule_is_rejected() -> None:
    """A rule expressing no period is a no-op and refused at construction."""
    with pytest.raises(RetentionError) as caught:
        BucketLifecycleRule("inputs/")
    assert caught.value.code == "lifecycle_rule_empty"


@pytest.mark.asyncio
async def test_failed_attempt_expires_while_deliverable_survives() -> None:
    """S12-T03: the shared prefix does not cost the deliverable its period."""
    store = MemoryRetentionStore(
        attempt("job-failed", INCOMPLETE, EPOCH)
        | attempt("job-committed", COMPLETE, EPOCH)
    )
    report = await RetentionReconciler(store).run(
        policy(), EPOCH + timedelta(days=20), dry_run=False
    )
    assert not any(key.startswith("outputs/job-failed/") for key in store.objects)
    survived = [k for k in store.objects if k.startswith("outputs/job-committed/")]
    assert len(survived) == 4
    assert report.deleted_by_class[RetainedClass.FAILED_ATTEMPT] == 3
    assert report.skipped_by_class[RetainedClass.DELIVERABLE] == 1
    assert report.clean


@pytest.mark.asyncio
async def test_committed_attempt_expires_with_its_prompt_evidence() -> None:
    """S12-T04: past the deliverable period the whole attempt goes.

    generation.bin carries the prompt text, so the prompt is destroyed with
    the deliverable it belonged to rather than on a clock of its own.
    """
    store = MemoryRetentionStore(attempt("job-old", COMPLETE, EPOCH))
    await RetentionReconciler(store).run(
        policy(), EPOCH + timedelta(days=400), dry_run=False
    )
    assert store.objects == {}
    assert "outputs/job-old/attempt-1/generation.bin" in store.deleted


@pytest.mark.asyncio
async def test_unreadable_marker_retains_the_attempt() -> None:
    """S12-T05: an attempt whose state is unknown is never deleted."""
    store = MemoryRetentionStore(attempt("job-1", INCOMPLETE, EPOCH), head_raises=True)
    report = await RetentionReconciler(store).run(
        policy(), EPOCH + timedelta(days=20), dry_run=False
    )
    assert store.deleted == []
    assert report.skipped_by_class[RetainedClass.DELIVERABLE] == 1


@pytest.mark.asyncio
async def test_dry_run_is_the_default_and_deletes_nothing() -> None:
    """S12-T07: expiry must be opted into, never arrived at by omission."""
    store = MemoryRetentionStore(attempt("job-1", INCOMPLETE, EPOCH))
    report = await RetentionReconciler(store).run(policy(), EPOCH + timedelta(days=20))
    assert store.deleted == []
    assert store.objects != {}
    assert report.dry_run
    assert report.deleted_by_class[RetainedClass.FAILED_ATTEMPT] == 3


@pytest.mark.asyncio
async def test_classes_matching_nothing_are_reported() -> None:
    """S12-T08: a class with no objects is stated, not silently absent."""
    report = await RetentionReconciler(MemoryRetentionStore({})).run(policy(), EPOCH)
    assert RetainedClass.SCRATCH in report.classes_with_no_objects
    assert RetainedClass.PROMPT_EVIDENCE in report.classes_with_no_objects
    assert report.evaluated == 0


@pytest.mark.asyncio
async def test_refused_delete_is_counted_and_the_sweep_continues() -> None:
    """A provider refusal never reads as a clean sweep."""
    store = MemoryRetentionStore(attempt("job-1", INCOMPLETE, EPOCH))
    store.refuse = {"outputs/job-1/attempt-1/video.bin"}
    report = await RetentionReconciler(store).run(
        policy(), EPOCH + timedelta(days=20), dry_run=False
    )
    assert report.failures == 1
    assert not report.clean
    assert len(store.deleted) == 2


@pytest.mark.asyncio
async def test_attempt_is_dated_by_its_oldest_object() -> None:
    """One late write must not hold a whole namespace past its period."""
    objects = attempt("job-1", INCOMPLETE, EPOCH)
    objects["outputs/job-1/attempt-1/late.bin"] = EPOCH + timedelta(days=19)
    store = MemoryRetentionStore(objects)
    await RetentionReconciler(store).run(
        policy(), EPOCH + timedelta(days=20), dry_run=False
    )
    assert store.objects == {}


def test_only_the_reaper_can_delete() -> None:
    """Delete capability is confined to the one control-plane role."""
    holders = {
        role
        for role, capabilities in ROLE_CAPABILITIES.items()
        if StorageCapability.DELETE_FILES in capabilities
    }
    assert holders == {CredentialRole.RETENTION_REAPER}


def test_reaper_credential_reaches_no_worker_process() -> None:
    """S12-T06: no worker subprocess may be handed a deleting credential."""
    for allowed in SECRET_VARIABLES.values():
        assert not REAPER_VARIABLES.intersection(allowed)


def test_missing_delete_capability_fails_before_the_sweep() -> None:
    """A credential that cannot delete fails up front, not partway through."""
    with pytest.raises(RetentionError) as caught:
        require_delete_capability(False)
    assert caught.value.code == "retention_credential_cannot_delete"
