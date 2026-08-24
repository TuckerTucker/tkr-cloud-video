"""Reference-conditioned workflow graph and binding-map tests.

The binder mutates only declared node inputs and refuses a binding whose input
name is absent from the pinned graph, so a reference cannot be bound into a node
that has no image input. The published text-to-video graph drives
``MiniMaxH3ImageToVideo`` with neither keyframe attached, which is exactly what
text-to-video is on this model set — and exactly why a reference has nowhere to
land there. What is proven here is that the reference release publishes a graph
whose four reference inputs exist, that the binding map reaches them, and that
the binding vocabulary stays release-approved rather than free.

The node and socket names asserted below are read from the ComfyUI revision the
worker image pins; ``release-assets/minimax-h3-ref2v/node-signature.md`` records
which files they were read from and how to re-check them.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tkr_cloud_video.artifacts.models import WorkflowBindingSource
from tkr_cloud_video.jobs.contracts import (
    ReferenceToVideoRequest,
    parse_generation_request,
)
from tkr_cloud_video.jobs.workflow_binder import (
    ParameterBinding,
    WorkflowBinder,
    WorkflowBindingError,
)
from tkr_cloud_video.release.catalog import prepare_model_set
from tkr_cloud_video.security.validation import Sha256Digest

ROOT = Path(__file__).resolve().parents[2]
REFERENCE_RELEASE = ROOT / "release-assets/minimax-h3-ref2v"
TEXT_RELEASE = ROOT / "release-assets/minimax-h3-t2v"

CONDITIONING_NODE = "105"
REFERENCE_LOADER_NODES = ("201", "202", "203", "204")
REFERENCE_SOCKETS = ("ref_image_0", "ref_image_1", "ref_image_2", "ref_image_3")
REFERENCE_SOURCES = ("input_0", "input_1", "input_2", "input_3")
STAGED_PATHS = (
    "job/inputs/input-0.png",
    "job/inputs/input-1.png",
    "job/inputs/input-2.png",
    "job/inputs/input-3.png",
)
# The trained envelope for the reference model set is registered by its own
# slice. Binding does not consult it, so the request names the registered
# text-to-video set rather than depending on work that has not landed.
MODEL_SET_ID = "minimax-h3-t2v-int8-20260809"


def load_graph(directory: Path = REFERENCE_RELEASE) -> dict[str, Any]:
    """Return the published API graph as a mutable document."""
    document: dict[str, Any] = json.loads(
        (directory / "workflow-api.json").read_bytes()
    )
    return document


def catalog_bindings(directory: Path = REFERENCE_RELEASE) -> tuple[Any, ...]:
    """Return the release-validated binding map for a published catalog."""
    prepared = prepare_model_set(directory / "catalog.json", "approval-1")
    workflow = next(entry for entry in prepared.manifest.artifacts if entry.bindings)
    return workflow.bindings


def bindings_as_parameters(
    directory: Path = REFERENCE_RELEASE,
) -> tuple[ParameterBinding, ...]:
    """Return the binding map in the shape the binder consumes."""
    return tuple(
        ParameterBinding(binding.source, binding.node_id, binding.input_name)
        for binding in catalog_bindings(directory)
    )


def reference_request(count: int = 4) -> ReferenceToVideoRequest:
    """Build a validated reference request carrying ``count`` references."""
    request = parse_generation_request(
        {
            "mode": "reference-to-video",
            "workflow_id": "h3-ref2v-1",
            "model_set_id": MODEL_SET_ID,
            "prompt": "A safe synthetic prompt referring to <Picture 1>",
            "seed": 42,
            "references": [
                {"object_key": f"inputs/principal/reference-{index}.png"}
                for index in range(count)
            ],
        }
    )
    assert isinstance(request, ReferenceToVideoRequest)
    return request


def runtime_values(count: int = 4) -> dict[str, str]:
    """Build the staged-input values the application hands the binder."""
    values = {f"input_{index}": STAGED_PATHS[index] for index in range(count)}
    values["prompt"] = "A safe synthetic prompt referring to <Picture 1>"
    values["output_prefix"] = "job-1/attempt-1/video"
    return values


def bind(
    graph: dict[str, Any],
    bindings: tuple[ParameterBinding, ...] | None = None,
    count: int = 4,
) -> dict[str, Any]:
    """Bind a request into a graph through its verified canonical bytes."""
    content = json.dumps(graph).encode()
    return WorkflowBinder().bind(
        content,
        Sha256Digest(hashlib.sha256(content).hexdigest()),
        bindings if bindings is not None else bindings_as_parameters(),
        reference_request(count),
        runtime_values(count),
    )


def test_published_text_graph_declares_no_reference_input() -> None:
    """The premise: today's release has nowhere for a reference to land.

    ``MiniMaxH3ImageToVideo`` declares ``first_frame`` and ``last_frame`` as its
    only image inputs and both are optional; the published text-to-video graph
    attaches neither. This is what the reference release exists to fix, so it is
    asserted rather than described.
    """
    conditioning = load_graph(TEXT_RELEASE)["104"]
    assert conditioning["class_type"] == "MiniMaxH3ImageToVideo"
    assert "first_frame" not in conditioning["inputs"]
    assert "last_frame" not in conditioning["inputs"]
    sources = {binding.source for binding in catalog_bindings(TEXT_RELEASE)}
    assert not sources & {WorkflowBindingSource(name) for name in REFERENCE_SOURCES}


def test_reference_graph_declares_four_reference_inputs() -> None:
    """The published reference graph wires four loaders into the ref node."""
    graph = load_graph()
    conditioning = graph[CONDITIONING_NODE]
    assert conditioning["class_type"] == "MiniMaxH3ReferenceToVideo"
    for node_id, socket in zip(REFERENCE_LOADER_NODES, REFERENCE_SOCKETS, strict=True):
        assert graph[node_id]["class_type"] == "LoadImage"
        assert "image" in graph[node_id]["inputs"]
        assert conditioning["inputs"][socket] == [node_id, 0]


def test_reference_binding_map_stays_inside_the_release_allowlist() -> None:
    """S1-T03 (positive half): no new binding source is introduced."""
    sources = [binding.source for binding in catalog_bindings()]
    assert all(source in set(WorkflowBindingSource) for source in sources)
    assert [
        source.value for source in sources if source.value.startswith("input_")
    ] == list(REFERENCE_SOURCES)


def test_every_published_binding_targets_a_declared_input() -> None:
    """Each catalog binding names a node input that exists in the graph."""
    graph = load_graph()
    for binding in catalog_bindings():
        assert binding.input_name in graph[binding.node_id]["inputs"]


def test_four_references_reach_four_declared_node_inputs() -> None:
    """S1-T02: each staged reference lands on its own declared node input."""
    bound = bind(load_graph())
    landed = [bound[node_id]["inputs"]["image"] for node_id in REFERENCE_LOADER_NODES]
    assert landed == list(STAGED_PATHS)
    assert len(set(landed)) == len(REFERENCE_LOADER_NODES)
    conditioning = bound[CONDITIONING_NODE]["inputs"]
    for node_id, socket in zip(REFERENCE_LOADER_NODES, REFERENCE_SOCKETS, strict=True):
        assert conditioning[socket] == [node_id, 0]


def test_binding_leaves_the_published_graph_untouched() -> None:
    """Binding returns a bound copy; the release artifact is immutable."""
    graph = load_graph()
    before = copy.deepcopy(graph)
    bind(graph)
    assert graph == before
    assert load_graph() == before


@pytest.mark.parametrize("absent", REFERENCE_SOCKETS)
def test_binding_fails_when_the_reference_input_is_absent(absent: str) -> None:
    """S1-T01: a graph missing a reference input refuses the bind.

    The refusal is what matters: a graph that cannot receive a reference must
    not run as though it had. The failing socket is removed from the
    conditioning node and its loader dropped, which is the shape a graph
    published without that slot would have.
    """
    graph = load_graph()
    index = REFERENCE_SOCKETS.index(absent)
    del graph[CONDITIONING_NODE]["inputs"][absent]
    del graph[REFERENCE_LOADER_NODES[index]]
    with pytest.raises(WorkflowBindingError) as caught:
        bind(graph)
    assert caught.value.code in {"binding_node_missing", "binding_input_missing"}
    assert caught.value.retryable is False


def test_binding_fails_naming_the_missing_input_on_the_loader() -> None:
    """S1-T01: the loader node survives but its image input does not.

    This isolates ``binding_input_missing`` from ``binding_node_missing``: the
    node the binding names is present, the input it names is not, and the bind
    is refused before prompt submission rather than generating without the
    reference.
    """
    graph = load_graph()
    del graph[REFERENCE_LOADER_NODES[3]]["inputs"]["image"]
    with pytest.raises(WorkflowBindingError) as caught:
        bind(graph)
    assert caught.value.code == "binding_input_missing"
    assert caught.value.retryable is False


def test_reference_bindings_cannot_be_applied_to_the_text_graph() -> None:
    """The reference binding map is refused against the text-to-video graph."""
    with pytest.raises(WorkflowBindingError) as caught:
        bind(load_graph(TEXT_RELEASE))
    assert caught.value.code == "binding_node_missing"
    assert caught.value.retryable is False


def test_binding_is_refused_when_the_graph_digest_differs(tmp_path: Path) -> None:
    """S1-TF: a graph whose digest is not the manifest's never reaches submit."""
    graph = load_graph()
    published = (REFERENCE_RELEASE / "workflow-api.json").read_bytes()
    tampered = json.dumps(graph).encode()
    with pytest.raises(WorkflowBindingError) as caught:
        WorkflowBinder().bind(
            tampered,
            Sha256Digest(hashlib.sha256(published).hexdigest()),
            bindings_as_parameters(),
            reference_request(),
            runtime_values(),
        )
    assert caught.value.code == "workflow_digest_mismatch"
    assert caught.value.retryable is False


@pytest.mark.parametrize("source", ["input_4", "reference_0", "ref_image_0"])
def test_catalog_refuses_a_binding_source_outside_the_allowlist(
    tmp_path: Path, source: str
) -> None:
    """S1-T03: publication fails when the binding vocabulary is not approved."""
    catalog = json.loads((REFERENCE_RELEASE / "catalog.json").read_bytes())
    catalog["workflow"]["bindings"][6]["source"] = source
    (tmp_path / "catalog.json").write_text(json.dumps(catalog))
    (tmp_path / "workflow-api.json").write_bytes(
        (REFERENCE_RELEASE / "workflow-api.json").read_bytes()
    )
    with pytest.raises(ValidationError):
        prepare_model_set(tmp_path / "catalog.json", "approval-1")


def test_catalog_refuses_two_bindings_aimed_at_one_node_input(
    tmp_path: Path,
) -> None:
    """Two references sharing a loader would silently drop one of them."""
    catalog = json.loads((REFERENCE_RELEASE / "catalog.json").read_bytes())
    catalog["workflow"]["bindings"][7]["node_id"] = REFERENCE_LOADER_NODES[0]
    (tmp_path / "catalog.json").write_text(json.dumps(catalog))
    (tmp_path / "workflow-api.json").write_bytes(
        (REFERENCE_RELEASE / "workflow-api.json").read_bytes()
    )
    with pytest.raises(ValidationError):
        prepare_model_set(tmp_path / "catalog.json", "approval-1")


def test_published_reference_catalog_renders() -> None:
    """The reference catalog is a valid, digest-addressed model set."""
    prepared = prepare_model_set(REFERENCE_RELEASE / "catalog.json", "approval-1")
    assert prepared.catalog.model_set_id == "minimax-h3-ref2v-int8-20260809"
    workflow = next(entry for entry in prepared.manifest.artifacts if entry.bindings)
    content = (REFERENCE_RELEASE / "workflow-api.json").read_bytes()
    assert workflow.sha256 == hashlib.sha256(content).hexdigest()
    assert workflow.sha256 in workflow.object_key


def test_node_signature_is_recorded_beside_the_graph() -> None:
    """The pinned revision and declared socket names are re-checkable."""
    signature = (REFERENCE_RELEASE / "node-signature.md").read_text()
    revision = "dec5d9450a5290bcf63430409ea41018e67f41c3"
    assert revision in signature
    assert revision in (ROOT / "Dockerfile").read_text()
    assert "comfy_extras/nodes_minimax_h3.py" in signature
    for socket in (*REFERENCE_SOCKETS, "ref_image_size", "audio_vae"):
        assert socket in signature
