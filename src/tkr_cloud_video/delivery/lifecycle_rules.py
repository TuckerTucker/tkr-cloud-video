"""Projection of the declared retention policy onto B2 lifecycle rules.

Only some retained classes can be expressed as a bucket rule. B2 accepts four
fields per rule — a prefix, days from uploading to hiding, days from hiding to
deleting, and days from starting to canceling unfinished large files — and it
resolves overlapping prefixes by applying the *smallest* non-null value for
each property.

That resolution rule is why this module refuses overlaps rather than emitting
them. Committed deliverables and failed attempts share the ``outputs/`` prefix,
so declaring both as rules would apply the failed-attempt period to committed
deliverables and delete them early. Those classes are enforced by the
reconciler instead, which decides class membership by heading the commit marker
rather than by matching a prefix.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Protocol

from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.delivery.lifecycle import LifecyclePolicy, RetainedClass

MAX_BUCKET_RULES: Final[int] = 100
INPUT_PREFIX: Final[str] = "inputs/"
OUTPUT_PREFIX: Final[str] = "outputs/"

RULE_EXPRESSIBLE: Final[frozenset[RetainedClass]] = frozenset(
    {
        RetainedClass.INPUT,
        RetainedClass.HIDDEN_VERSION,
        RetainedClass.MULTIPART,
    }
)
RECONCILER_ENFORCED: Final[frozenset[RetainedClass]] = frozenset(
    {
        RetainedClass.FAILED_ATTEMPT,
        RetainedClass.DELIVERABLE,
        RetainedClass.SCRATCH,
        RetainedClass.PROMPT_EVIDENCE,
    }
)


class RetentionError(AppError):
    """A retention rule set or sweep cannot be carried out safely."""


@dataclass(frozen=True, slots=True)
class BucketLifecycleRule:
    """One B2 lifecycle rule, in exactly the fields the provider accepts."""

    file_name_prefix: str
    days_from_uploading_to_hiding: int | None = None
    days_from_hiding_to_deleting: int | None = None
    days_from_starting_to_canceling_unfinished_large_files: int | None = None

    def __post_init__(self) -> None:
        """Require a rule that expresses at least one period."""
        if not any(
            (
                self.days_from_uploading_to_hiding,
                self.days_from_hiding_to_deleting,
                self.days_from_starting_to_canceling_unfinished_large_files,
            )
        ):
            raise RetentionError(
                "lifecycle_rule_empty",
                "a lifecycle rule must express at least one period",
                context={"rule": self.file_name_prefix},
            )


class LifecycleRuleSet:
    """Projects a declared policy onto the rules a bucket can carry."""

    def project(self, policy: LifecyclePolicy) -> tuple[BucketLifecycleRule, ...]:
        """Return the bucket rules expressing every rule-expressible class.

        Args:
            policy: The validated declared policy.

        Returns:
            One rule per distinct prefix. Classes the provider cannot express
            by prefix are absent by design and are enforced by the reconciler.

        Raises:
            RetentionError: If two classes would place different periods on one
                prefix, or if the rule count exceeds what a bucket accepts.

        """
        input_days = policy.period_for(RetainedClass.INPUT).days
        hidden_days = policy.period_for(RetainedClass.HIDDEN_VERSION).days
        multipart_days = policy.period_for(RetainedClass.MULTIPART).days
        rules = (
            # Inputs are hidden at their period and deleted immediately after,
            # so the object is unreachable on the declared day rather than
            # merely superseded on it.
            BucketLifecycleRule(
                file_name_prefix=INPUT_PREFIX,
                days_from_uploading_to_hiding=input_days,
                days_from_hiding_to_deleting=1,
            ),
            # Applies to every prefix, including outputs/. It carries no
            # uploading-to-hiding value, so it cannot shorten the period of any
            # live object under the smallest-value rule.
            BucketLifecycleRule(
                file_name_prefix="",
                days_from_hiding_to_deleting=hidden_days,
                days_from_starting_to_canceling_unfinished_large_files=(multipart_days),
            ),
        )
        self._reject_conflicting_overlap(rules)
        if len(rules) > MAX_BUCKET_RULES:
            raise RetentionError(
                "lifecycle_rule_budget_exceeded",
                "projected more lifecycle rules than a bucket accepts",
            )
        return rules

    def _reject_conflicting_overlap(
        self, rules: tuple[BucketLifecycleRule, ...]
    ) -> None:
        """Refuse rule pairs whose prefixes overlap with different periods.

        B2 applies the smallest non-null value for each property among all
        matching rules. Two overlapping prefixes carrying different
        uploading-to-hiding periods therefore silently enforce the shorter one,
        which is how a deliverable would be deleted on a failed-attempt clock.
        """
        for index, rule in enumerate(rules):
            for other in rules[index + 1 :]:
                if not self._overlaps(rule.file_name_prefix, other.file_name_prefix):
                    continue
                left = rule.days_from_uploading_to_hiding
                right = other.days_from_uploading_to_hiding
                if left is not None and right is not None and left != right:
                    raise RetentionError(
                        "lifecycle_rule_overlap_conflict",
                        "overlapping prefixes declare different retention periods",
                        context={"rule": other.file_name_prefix or "<bucket>"},
                    )

    @staticmethod
    def _overlaps(left: str, right: str) -> bool:
        """Return whether two rule prefixes can match a common object."""
        return left.startswith(right) or right.startswith(left)


class BucketRuleAdapter(Protocol):
    """Bucket-configuration port for reading and applying lifecycle rules.

    Requires a capability no runtime credential holds. It is operator-invoked
    and never constructed inside a worker or delivery process.
    """

    async def read(self) -> tuple[BucketLifecycleRule, ...]:
        """Return the rules currently in force on the bucket."""
        ...

    async def apply(self, rules: tuple[BucketLifecycleRule, ...]) -> None:
        """Replace the bucket's rules with the supplied set."""
        ...


def detect_drift(
    declared: tuple[BucketLifecycleRule, ...],
    in_force: tuple[BucketLifecycleRule, ...],
) -> None:
    """Fail when the rules in force differ from the declared projection.

    Args:
        declared: Rules the policy projects.
        in_force: Rules the bucket reports.

    Raises:
        RetentionError: If any prefix is missing, unexpected, or carries
            different periods. Drift is named by prefix so an operator learns
            which class stopped being enforced, not merely that something did.

    """
    declared_by_prefix = {rule.file_name_prefix: rule for rule in declared}
    in_force_by_prefix = {rule.file_name_prefix: rule for rule in in_force}
    missing = sorted(set(declared_by_prefix).difference(in_force_by_prefix))
    if missing:
        raise RetentionError(
            "lifecycle_rule_missing",
            "a declared lifecycle rule is not in force on the bucket",
            context={"rule": ",".join(prefix or "<bucket>" for prefix in missing)},
        )
    unexpected = sorted(set(in_force_by_prefix).difference(declared_by_prefix))
    if unexpected:
        raise RetentionError(
            "lifecycle_rule_unexpected",
            "the bucket carries a lifecycle rule the policy does not declare",
            context={"rule": ",".join(prefix or "<bucket>" for prefix in unexpected)},
        )
    for prefix, rule in sorted(declared_by_prefix.items()):
        if in_force_by_prefix[prefix] != rule:
            raise RetentionError(
                "lifecycle_rule_diverged",
                "a lifecycle rule in force differs from the declared period",
                context={"rule": prefix or "<bucket>"},
            )
