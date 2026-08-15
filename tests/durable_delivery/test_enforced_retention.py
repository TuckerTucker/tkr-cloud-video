"""Retention policy loading, class completeness, and rejection contracts.

Slice 11 coverage. These tests assert that a policy is loaded and validated;
they deliberately do not claim that anything is deleted. The enforcing
mechanisms arrive in slices 12 and 13, and the coverage proving observable
absence after a period elapses arrives in slice 15.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import pytest

from tkr_cloud_video.delivery.lifecycle import (
    ConfigSource,
    FileConfigSource,
    LifecyclePolicy,
    LifecyclePolicyLoader,
    RetainedClass,
    RetentionPolicyError,
    RetentionRule,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SHIPPED_POLICY = PROJECT_ROOT / "config" / "lifecycle.json"


@dataclass
class MemoryConfigSource(ConfigSource):
    """In-memory policy document source."""

    document: Mapping[str, object]
    reads: int = field(default=0)

    def read(self) -> Mapping[str, object]:
        """Return the configured document."""
        self.reads += 1
        return self.document


def complete_rules() -> dict[str, int]:
    """Build a declared rules object covering every retained class."""
    return {item.value: 7 for item in RetainedClass}


def document(**overrides: object) -> dict[str, object]:
    """Build a well-formed policy document with optional overrides."""
    payload: dict[str, object] = {"version": "1", "rules": complete_rules()}
    payload.update(overrides)
    return payload


def test_loader_returns_declared_version_and_every_period() -> None:
    """S11-T01: a complete document loads with its version and all periods."""
    source = MemoryConfigSource(document(version="4"))
    policy = LifecyclePolicyLoader().load(source)
    assert policy.version == "4"
    assert len(policy.rules) == len(RetainedClass)
    assert {rule.retained_class for rule in policy.rules} == set(RetainedClass)
    assert source.reads == 1


def test_loader_rejects_document_omitting_a_class() -> None:
    """S11-T02: an omitted class fails construction and names the class."""
    rules = complete_rules()
    del rules[RetainedClass.SCRATCH.value]
    with pytest.raises(RetentionPolicyError) as caught:
        LifecyclePolicyLoader().load(MemoryConfigSource(document(rules=rules)))
    assert caught.value.code == "retention_class_missing"
    assert caught.value.context["rule"] == RetainedClass.SCRATCH.value


def test_loader_rejects_document_naming_an_unknown_class() -> None:
    """S11-T02: an unknown class name is refused rather than ignored."""
    rules = complete_rules() | {"transcripts": 30}
    with pytest.raises(RetentionPolicyError) as caught:
        LifecyclePolicyLoader().load(MemoryConfigSource(document(rules=rules)))
    assert caught.value.code == "retention_class_unknown"
    assert caught.value.context["rule"] == "transcripts"


def test_prompt_evidence_carries_a_declared_period() -> None:
    """S11-T03: the class no bucket prefix can reach resolves to a period."""
    policy = LifecyclePolicyLoader().load(MemoryConfigSource(document()))
    assert policy.period_for(RetainedClass.PROMPT_EVIDENCE) == timedelta(days=7)


def test_shipped_policy_document_loads_unmodified() -> None:
    """S11-T04: the shipped document loads.

    This is the assertion that would have failed on every day of the previous
    implementation, when the document was read by no code at all.
    """
    policy = LifecyclePolicyLoader().load(
        FileConfigSource(SHIPPED_POLICY, PROJECT_ROOT)
    )
    declared = json.loads(SHIPPED_POLICY.read_text())
    assert policy.version == declared["version"]
    for name, days in declared["rules"].items():
        assert policy.period_for(RetainedClass(name)) == timedelta(days=days)


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"version": ""}, "retention_version_missing"),
        ({"version": 1}, "retention_version_missing"),
        ({"rules": []}, "retention_rules_missing"),
        ({"rules": complete_rules() | {"scratch": "2"}}, "retention_period_malformed"),
        ({"rules": complete_rules() | {"scratch": True}}, "retention_period_malformed"),
        ({"rules": complete_rules() | {"scratch": 0}}, "retention_period_out_of_range"),
        (
            {"rules": complete_rules() | {"scratch": 3651}},
            "retention_period_out_of_range",
        ),
    ],
)
def test_malformed_documents_fail_closed(
    overrides: dict[str, object], code: str
) -> None:
    """S11-TF: every declared failure is total, with no partial policy."""
    with pytest.raises(RetentionPolicyError) as caught:
        LifecyclePolicyLoader().load(MemoryConfigSource(document(**overrides)))
    assert caught.value.code == code
    assert caught.value.retryable is False


def test_unreadable_document_synthesizes_no_default(tmp_path: Path) -> None:
    """S11-TF: absence yields an error, never an unbounded default period."""
    source = FileConfigSource(tmp_path / "missing.json", tmp_path)
    with pytest.raises(RetentionPolicyError) as caught:
        LifecyclePolicyLoader().load(source)
    assert caught.value.code == "retention_config_unreadable"


def test_config_path_escaping_the_root_is_rejected(tmp_path: Path) -> None:
    """S11-TF: the policy path is validated before any read is attempted."""
    with pytest.raises(RetentionPolicyError) as caught:
        FileConfigSource(tmp_path.parent / "elsewhere.json", tmp_path)
    assert caught.value.code == "retention_config_path_rejected"


def test_composition_without_a_retention_store_cannot_delete() -> None:
    """Slice 15: a process that must not delete composes without the means.

    The delivery and worker compositions get no retention store, so expiry and
    erasure are structurally absent rather than merely unused.
    """
    from tests.durable_delivery.test_subject_requests import (
        Authorizer,
        held_store,
    )
    from tests.durable_delivery.test_verified_result_commit import (
        MemoryResultStore,
    )
    from tkr_cloud_video.durable_delivery.composition import (
        DeliveryDependencies,
        compose_durable_delivery,
    )

    loaded = LifecyclePolicyLoader().load(MemoryConfigSource(document()))
    base = {
        "store": MemoryResultStore(),
        "authorizer": Authorizer(True),
        "repository": None,
        "signer": None,
        "clock": None,
        "signed_link_ttl_seconds": 300,
        "lifecycle_policy": loaded,
    }
    without = compose_durable_delivery(DeliveryDependencies(**base))  # type: ignore[arg-type]
    assert not without.can_delete
    assert without.reconciler is None and without.subject_requests is None

    with_store = compose_durable_delivery(
        DeliveryDependencies(**base, retention_store=held_store())  # type: ignore[arg-type]
    )
    assert with_store.can_delete
    assert with_store.declared_rules


def test_duplicate_class_is_rejected_at_construction() -> None:
    """A policy built in code cannot declare one class twice."""
    rules = tuple(RetentionRule(item, 7) for item in RetainedClass)
    with pytest.raises(RetentionPolicyError) as caught:
        LifecyclePolicy("1", (*rules, RetentionRule(RetainedClass.SCRATCH, 9)))
    assert caught.value.code == "retention_class_duplicated"
