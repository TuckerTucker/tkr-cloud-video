"""Pinned grammar table contracts."""

from __future__ import annotations

import pytest

from tkr_cloud_video.prompting.grammar import (
    CAMERA_AMPLITUDES,
    CAMERA_MOTION_TYPES,
    CAMERA_MOTIONS,
    CAMERA_SPEEDS,
    GRAMMAR_DIGEST,
    MODES,
    SECTION_ORDER,
    VOCABULARIES,
    PromptGrammarDefinitionError,
    PromptGrammarIntegrityError,
    Vocabulary,
    grammar_fingerprint,
    verify_grammar_integrity,
)


def test_pinned_digest_matches_the_tables() -> None:
    verify_grammar_integrity()
    assert grammar_fingerprint() == GRAMMAR_DIGEST


def test_a_table_edit_without_a_restated_digest_fails_closed() -> None:
    with pytest.raises(PromptGrammarIntegrityError) as raised:
        verify_grammar_integrity(expected="0" * 64)

    assert raised.value.code == "grammar_digest_mismatch"
    assert set(raised.value.context) <= {"field", "rule"}


def test_fingerprint_is_stable_across_calls() -> None:
    assert grammar_fingerprint() == grammar_fingerprint()


@pytest.mark.parametrize("table", VOCABULARIES, ids=lambda table: table.name)
def test_every_vocabulary_is_non_empty_and_unique(table: Vocabulary) -> None:
    assert table.values
    assert len(set(table.values)) == len(table.values)


def test_empty_vocabulary_is_rejected() -> None:
    with pytest.raises(PromptGrammarDefinitionError) as raised:
        Vocabulary(name="empty", values=())

    assert raised.value.code == "grammar_table_empty"


def test_duplicate_vocabulary_value_is_rejected() -> None:
    with pytest.raises(PromptGrammarDefinitionError) as raised:
        Vocabulary(name="repeats", values=("a", "a"))

    assert raised.value.code == "grammar_table_duplicate_value"


def test_omitted_default_may_not_also_be_renderable() -> None:
    with pytest.raises(PromptGrammarDefinitionError) as raised:
        Vocabulary(name="conflicted", values=("medium",), omitted_default="medium")

    assert raised.value.code == "grammar_default_has_wire_form"


def test_camera_modifiers_declare_an_omitted_default() -> None:
    """Medium amplitude and normal speed render as nothing, per the guide."""
    assert CAMERA_AMPLITUDES.omitted_default == "medium"
    assert CAMERA_SPEEDS.omitted_default == "normal"
    assert not CAMERA_AMPLITUDES.permits("medium")
    assert not CAMERA_SPEEDS.permits("normal")


def test_camera_motion_labels_and_type_table_agree() -> None:
    assert CAMERA_MOTION_TYPES.values == tuple(m.label for m in CAMERA_MOTIONS)
    assert all(motion.phrase for motion in CAMERA_MOTIONS)


def test_permits_rejects_a_value_outside_the_table() -> None:
    assert CAMERA_MOTION_TYPES.permits("Push In")
    assert not CAMERA_MOTION_TYPES.permits("Dolly Sideways")


def test_every_mode_declares_a_section_order() -> None:
    assert set(SECTION_ORDER) == set(MODES)
    assert all(SECTION_ORDER[mode] for mode in MODES)
    assert SECTION_ORDER["Ref2VA"][0] == "subject_definitions"
    assert SECTION_ORDER["T2VA"][0] == "integrated_multimodal_description"
