"""Versioned object-class retention policy and fixture evaluator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


class ObjectClass(StrEnum):
    """Object lifecycle classes with distinct retention purposes."""

    INPUT = "input"
    FAILED_ATTEMPT = "failed-attempt"
    SCRATCH = "scratch"
    DELIVERABLE = "deliverable"
    HIDDEN_VERSION = "hidden-version"
    MULTIPART = "multipart"


@dataclass(frozen=True, slots=True)
class RetentionRule:
    """One versioned object class retention rule."""

    object_class: ObjectClass
    retain_days: int

    def __post_init__(self) -> None:
        """Require a positive bounded retention period."""
        if not 1 <= self.retain_days <= 3650:
            raise ValueError("retention days must be between 1 and 3650")


@dataclass(frozen=True, slots=True)
class LifecyclePolicy:
    """Complete versioned retention policy."""

    version: str
    rules: tuple[RetentionRule, ...]

    def __post_init__(self) -> None:
        """Require exactly one rule for every object class."""
        classes = [rule.object_class for rule in self.rules]
        if set(classes) != set(ObjectClass) or len(classes) != len(set(classes)):
            raise ValueError("lifecycle policy must cover every object class once")

    def expired(
        self, object_class: ObjectClass, created_at: datetime, evaluated_at: datetime
    ) -> bool:
        """Evaluate a dated fixture against its exact class rule."""
        rule = next(item for item in self.rules if item.object_class is object_class)
        return evaluated_at >= created_at + timedelta(days=rule.retain_days)
