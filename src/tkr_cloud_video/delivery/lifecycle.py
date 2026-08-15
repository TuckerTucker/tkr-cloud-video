"""Versioned retention policy covering every class of retained data.

The policy is the single declaration of how long each class is kept. It is
loaded from configuration rather than constructed at its call sites so that the
periods an operator approved and the periods a mechanism enforces cannot drift
apart.

A policy object deletes nothing. It answers how long a class is retained and
whether a dated object has passed that period; the mechanisms that act on those
answers are the bucket lifecycle adapter and the prompt-evidence processor.
Treating a policy that evaluates correctly as a policy that is enforced is the
specific mistake this module's history records: the periods below were declared
and audited by fixtures for the whole life of the previous implementation while
nothing ever deleted an object.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol

from tkr_cloud_video.core.errors import AppError

MIN_RETAIN_DAYS: Final[int] = 1
MAX_RETAIN_DAYS: Final[int] = 3650


class RetentionPolicyError(AppError):
    """A declared retention policy is absent, incomplete, or out of range."""


class RetainedClass(StrEnum):
    """Every class of retained data carrying its own retention period.

    Supersedes the object-store-only class set. ``PROMPT_EVIDENCE`` is not an
    object under a storage prefix: caller-supplied prompt text is committed to
    job evidence, so no bucket lifecycle rule can reach it and it needs a
    period here to be erasable at all.
    """

    INPUT = "input"
    FAILED_ATTEMPT = "failed-attempt"
    SCRATCH = "scratch"
    DELIVERABLE = "deliverable"
    HIDDEN_VERSION = "hidden-version"
    MULTIPART = "multipart"
    PROMPT_EVIDENCE = "prompt-evidence"


@dataclass(frozen=True, slots=True)
class RetentionRule:
    """One retained class and its bounded retention period."""

    retained_class: RetainedClass
    retain_days: int

    def __post_init__(self) -> None:
        """Require a positive bounded retention period."""
        if not MIN_RETAIN_DAYS <= self.retain_days <= MAX_RETAIN_DAYS:
            raise RetentionPolicyError(
                "retention_period_out_of_range",
                "retention days must be between 1 and 3650",
                context={"rule": self.retained_class.value},
            )


@dataclass(frozen=True, slots=True)
class LifecyclePolicy:
    """Complete versioned retention policy, total over every retained class."""

    version: str
    rules: tuple[RetentionRule, ...]

    def __post_init__(self) -> None:
        """Require exactly one rule for every retained class.

        Totality is enforced here rather than at lookup so that a class added
        to the enum without a configured period fails at construction. A
        missing period must never degrade to an unbounded default: that is the
        shape in which data quietly persists forever.
        """
        declared = [rule.retained_class for rule in self.rules]
        missing = set(RetainedClass).difference(declared)
        if missing:
            raise RetentionPolicyError(
                "retention_class_missing",
                "lifecycle policy must declare a period for every retained class",
                context={"rule": ",".join(sorted(item.value for item in missing))},
            )
        if len(declared) != len(set(declared)):
            raise RetentionPolicyError(
                "retention_class_duplicated",
                "lifecycle policy must declare each retained class exactly once",
            )

    def period_for(self, retained_class: RetainedClass) -> timedelta:
        """Return the declared retention period for one class.

        Args:
            retained_class: The class whose period is required.

        Returns:
            The declared period. Total by construction, so absence of a rule is
            impossible here rather than handled here.

        """
        rule = next(
            item for item in self.rules if item.retained_class is retained_class
        )
        return timedelta(days=rule.retain_days)

    def expired(
        self,
        retained_class: RetainedClass,
        created_at: datetime,
        evaluated_at: datetime,
    ) -> bool:
        """Return whether a dated item has passed its class period.

        Args:
            retained_class: The class the item belongs to.
            created_at: When the item was written.
            evaluated_at: The moment the question is asked.

        Returns:
            True when the period has elapsed. This is an answer, not an action:
            nothing is deleted by asking.

        """
        return evaluated_at >= created_at + self.period_for(retained_class)


class ConfigSource(Protocol):
    """Source of the raw declared retention policy document."""

    def read(self) -> Mapping[str, object]:
        """Return the parsed policy document."""
        ...


class FileConfigSource:
    """Reads the declared policy from a JSON file inside the project root."""

    def __init__(self, path: Path, root: Path) -> None:
        """Initialize with the policy path and the root it must stay within.

        Args:
            path: Location of the policy document.
            root: Directory the resolved path may not escape.

        Raises:
            RetentionPolicyError: If the path escapes the root.

        """
        resolved = path.resolve()
        if not resolved.is_relative_to(root.resolve()):
            raise RetentionPolicyError(
                "retention_config_path_rejected",
                "lifecycle policy path escapes the project root",
            )
        self._path = resolved

    def read(self) -> Mapping[str, object]:
        """Return the parsed policy document.

        Returns:
            The decoded document.

        Raises:
            RetentionPolicyError: If the document is unreadable or malformed.
                No default policy is synthesized, because a synthesized period
                would enforce something nobody approved.

        """
        try:
            payload = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise RetentionPolicyError(
                "retention_config_unreadable",
                "lifecycle policy document could not be read",
                cause=error,
            ) from error
        if not isinstance(payload, dict):
            raise RetentionPolicyError(
                "retention_config_malformed",
                "lifecycle policy document must be an object",
            )
        return payload


class LifecyclePolicyLoader:
    """Builds a validated policy from a declared configuration document."""

    def load(self, source: ConfigSource) -> LifecyclePolicy:
        """Parse and validate the declared policy.

        Args:
            source: Injected source of the raw policy document.

        Returns:
            The validated policy.

        Raises:
            RetentionPolicyError: If the document is malformed, names an
                unknown class, or omits a declared one. Failure is total: no
                partial policy is returned.

        """
        document = source.read()
        version = document.get("version")
        if not isinstance(version, str) or not version.strip():
            raise RetentionPolicyError(
                "retention_version_missing",
                "lifecycle policy must declare a version",
            )
        declared = document.get("rules")
        if not isinstance(declared, dict):
            raise RetentionPolicyError(
                "retention_rules_missing",
                "lifecycle policy must declare a rules object",
            )
        return LifecyclePolicy(version, self._rules(declared))

    def _rules(self, declared: Mapping[str, object]) -> tuple[RetentionRule, ...]:
        """Convert declared entries to rules, rejecting unknown class names."""
        known = {item.value: item for item in RetainedClass}
        unknown = sorted(set(declared).difference(known))
        if unknown:
            raise RetentionPolicyError(
                "retention_class_unknown",
                "lifecycle policy names a class that does not exist",
                context={"rule": ",".join(unknown)},
            )
        rules: list[RetentionRule] = []
        for name, days in declared.items():
            if not isinstance(days, int) or isinstance(days, bool):
                raise RetentionPolicyError(
                    "retention_period_malformed",
                    "retention period must be a whole number of days",
                    context={"rule": name},
                )
            rules.append(RetentionRule(known[name], days))
        return tuple(rules)
