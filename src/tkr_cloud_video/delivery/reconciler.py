"""Expiry of output objects whose class no bucket rule can distinguish.

Committed deliverables and failed attempts share the ``outputs/`` prefix, and a
prefix rule cannot tell them apart. The distinguisher is the commit marker: an
attempt with a ``result.json`` is a committed deliverable, and one without it
never completed.

Two properties matter more than throughput here. The sweep fails closed toward
retention — an attempt whose state cannot be established is kept, because a
wrong deletion is unrecoverable and a wrong retention is not. And it defaults
to reporting rather than deleting, so a first run in an unfamiliar environment
cannot destroy anything by omission.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from tkr_cloud_video.delivery.lifecycle import LifecyclePolicy, RetainedClass
from tkr_cloud_video.delivery.lifecycle_rules import (
    OUTPUT_PREFIX,
    RECONCILER_ENFORCED,
    RetentionError,
)

COMMIT_MARKER: str = "result.json"


@dataclass(frozen=True, slots=True)
class StoredObject:
    """One object under an attempt prefix, with the time it was written."""

    key: str
    uploaded_at: datetime


@dataclass(frozen=True, slots=True)
class AttemptPrefix:
    """One attempt namespace and the classification decided for it."""

    prefix: str
    uploaded_at: datetime
    committed: bool
    objects: tuple[StoredObject, ...]

    @property
    def retained_class(self) -> RetainedClass:
        """Return the class whose period governs this attempt."""
        if self.committed:
            return RetainedClass.DELIVERABLE
        return RetainedClass.FAILED_ATTEMPT


class RetentionStore(Protocol):
    """Object-store port for the control-plane reaper.

    Distinct from :class:`~tkr_cloud_video.delivery.uploader.ResultStore`
    because it is the only port that deletes, and the only one bound to a
    credential holding ``deleteFiles``.
    """

    async def list_objects(self, prefix: str) -> tuple[StoredObject, ...]:
        """Return every object under a prefix with its upload time."""
        ...

    async def head_exists(self, key: str) -> bool:
        """Return whether one exact object exists."""
        ...

    async def delete(self, key: str) -> None:
        """Delete one exact object."""
        ...


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """Outcome of one sweep, complete over every class the reaper owns."""

    evaluated: int
    dry_run: bool
    deleted_by_class: Mapping[RetainedClass, int] = field(default_factory=dict)
    skipped_by_class: Mapping[RetainedClass, int] = field(default_factory=dict)
    classes_with_no_objects: tuple[RetainedClass, ...] = ()
    failures: int = 0

    @property
    def clean(self) -> bool:
        """Return whether the sweep completed with nothing left unresolved."""
        return self.failures == 0


class RetentionReconciler:
    """Expires attempt namespaces by class, deciding class from the marker."""

    def __init__(self, store: RetentionStore) -> None:
        """Initialize with a delete-capable control-plane store port."""
        self._store = store

    async def run(
        self,
        policy: LifecyclePolicy,
        evaluated_at: datetime,
        *,
        dry_run: bool = True,
    ) -> ReconcileReport:
        """Sweep the output namespace once and expire what has aged out.

        Args:
            policy: The declared retention policy.
            evaluated_at: The moment expiry is judged against.
            dry_run: When true, report what would be deleted and delete
                nothing. Defaults to true so deleting is always an explicit
                choice at the call site.

        Returns:
            A report complete over every class the reconciler owns, so a class
            that matched nothing is stated rather than silently missing.

        """
        attempts = await self._attempts(evaluated_at)
        deleted: dict[RetainedClass, int] = {}
        skipped: dict[RetainedClass, int] = {}
        failures = 0
        for attempt in attempts:
            retained = attempt.retained_class
            if not policy.expired(retained, attempt.uploaded_at, evaluated_at):
                skipped[retained] = skipped.get(retained, 0) + 1
                continue
            if dry_run:
                deleted[retained] = deleted.get(retained, 0) + len(attempt.objects)
                continue
            removed, errors = await self._delete_attempt(attempt)
            deleted[retained] = deleted.get(retained, 0) + removed
            failures += errors
        return ReconcileReport(
            evaluated=len(attempts),
            dry_run=dry_run,
            deleted_by_class=deleted,
            skipped_by_class=skipped,
            classes_with_no_objects=tuple(
                sorted(
                    RECONCILER_ENFORCED.difference(deleted).difference(skipped),
                    key=lambda item: item.value,
                )
            ),
            failures=failures,
        )

    async def _attempts(self, evaluated_at: datetime) -> tuple[AttemptPrefix, ...]:
        """Group stored objects into attempt namespaces and classify each."""
        objects = await self._store.list_objects(OUTPUT_PREFIX)
        grouped: dict[str, list[StoredObject]] = {}
        for stored in objects:
            prefix = _attempt_prefix(stored.key)
            if prefix is None:
                continue
            grouped.setdefault(prefix, []).append(stored)
        attempts: list[AttemptPrefix] = []
        for prefix, members in sorted(grouped.items()):
            attempts.append(
                AttemptPrefix(
                    prefix=prefix,
                    # The oldest object dates the attempt: expiring on the
                    # newest would let one late write hold a whole namespace
                    # past its period.
                    uploaded_at=min(item.uploaded_at for item in members),
                    committed=await self._committed(prefix),
                    objects=tuple(members),
                )
            )
        return tuple(attempts)

    async def _committed(self, prefix: str) -> bool:
        """Return whether an attempt carries its commit marker.

        An attempt whose state cannot be established is reported as committed,
        which retains it. Failing closed here means an unreadable marker costs
        storage rather than data.
        """
        try:
            return await self._store.head_exists(f"{prefix}{COMMIT_MARKER}")
        except Exception:
            return True

    async def _delete_attempt(self, attempt: AttemptPrefix) -> tuple[int, int]:
        """Delete one attempt's objects, continuing past provider refusals."""
        removed = 0
        errors = 0
        # The marker is removed last. If the sweep dies partway, what remains
        # still reads as a committed attempt rather than as a failed one, so a
        # resumed sweep re-derives the same class instead of a shorter period.
        ordered = sorted(
            attempt.objects, key=lambda item: item.key.endswith(COMMIT_MARKER)
        )
        for stored in ordered:
            try:
                await self._store.delete(stored.key)
            except Exception:
                errors += 1
                continue
            removed += 1
        return removed, errors


def _attempt_prefix(key: str) -> str | None:
    """Return the ``outputs/<job>/<attempt>/`` namespace owning a key."""
    if not key.startswith(OUTPUT_PREFIX):
        return None
    parts = key.split("/")
    if len(parts) < 4:
        return None
    return "/".join(parts[:3]) + "/"


def require_delete_capability(has_delete: bool) -> None:
    """Fail before the first delete rather than partway through a sweep.

    Args:
        has_delete: Whether the bound credential carries ``deleteFiles``.

    Raises:
        RetentionError: If the credential cannot delete.

    """
    if not has_delete:
        raise RetentionError(
            "retention_credential_cannot_delete",
            "the retention credential does not carry delete capability",
        )
