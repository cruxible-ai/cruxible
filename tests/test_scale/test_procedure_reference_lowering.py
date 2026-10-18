"""Graph Procedure lowering reads references, independently of unrelated state."""

from collections import Counter
from collections.abc import Iterator

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.authoring.models import ProcedureAuthoringPayload
from cruxible_client.contracts.documents import render_document
from cruxible_client.contracts.query.definitions import (
    query_definition_path,
    render_query_definition,
)
from cruxible_client.contracts.subjects import render_subject, subject_path
from cruxible_core.authoring.lowering import _lower_procedure
from cruxible_core.derived.derived_state import SnapshotTree
from tests.test_authoring.test_authoring_history_reuse import _intent
from tests.test_authoring.test_authoring_procedures import (
    _change_set_query,
    _list_contract_payload,
    _triage_definition,
    _world_holding_a_document,
)
from tests.test_claims.test_claims import _claim, _subject
from tests.test_scale._counters import counting, trace_sqlite
from tests.test_scale.test_scale_guard import RATIO, SLACK


@pytest.fixture(autouse=True)
def traced_sqlite(tmp_path):
    with trace_sqlite(tmp_path):
        yield


class ObservedTree(SnapshotTree):
    def __init__(self, blobs: dict[str, bytes]) -> None:
        super().__init__(blobs)
        self.work: Counter[str] = Counter()

    def __getitem__(self, path: str) -> bytes:
        self.work["tree_reads"] += 1
        return super().__getitem__(path)

    def __iter__(self) -> Iterator[str]:
        for path in super().__iter__():
            self.work["tree_paths"] += 1
            yield path


@pytest.mark.parametrize("candidate", [False, True])
def test_graph_procedure_reference_work_is_flat_at_ten_times_unrelated_state(tmp_path, candidate):
    from cruxible_client.contracts.claims import claim_path, render_claim
    from cruxible_client.contracts.documents import parse_document

    coordinator, _actor = _world_holding_a_document(tmp_path)
    instance = coordinator.instance
    base = instance.accepted_coordinate()
    original = dict(instance.immutable_tree_at(base.git_oid))
    document = parse_document(original["documents/design.json"], path="documents/design.json")
    query = _change_set_query()
    query_path = query_definition_path(query.identity.name)
    reference = {
        "tag": (
            "playbill-authoring-candidate-reference-v1"
            if candidate
            else "playbill-authoring-artifact-reference-v1"
        ),
        "role": "query",
        "target": query.identity.model_dump(mode="json"),
        "resolution": "candidate_in_change_set" if candidate else "accepted_at_intent_base",
    }
    payload: ProcedureAuthoringPayload = _list_contract_payload().model_copy(
        update={"definition": _triage_definition(query=reference)}
    )
    intent = _intent().model_copy(update={"payload": payload})
    measurements = []
    lowered_bytes = []
    for size in (10, 100):
        blobs = {**original, query_path: render_query_definition(query)}
        for index in range(size):
            shell = _subject().model_copy(
                update={
                    "identity": ArtifactIdentity(
                        kind="Subject", name=f"project.work_item/u-{index}"
                    ),
                    "subject_id": f"u-{index}",
                }
            )
            blobs[subject_path(shell.subject_kind, shell.subject_id)] = render_subject(shell)
            claim_id = f"CLM-{index:032x}"
            claim = _claim(
                claim_id=claim_id,
                capture_digest="sha256:" + "1" * 64,
                source_digest="sha256:" + "2" * 64,
                source_length=1,
            )
            blobs[claim_path(claim_id)] = render_claim(claim)
            unrelated = document.model_copy(update={"identity": f"document:unrelated-{index}"})
            blobs[f"documents/unrelated-{index}.json"] = render_document(unrelated)
        tree = ObservedTree(blobs)
        accepted = ObservedTree(original) if candidate else tree
        with counting(instance.root) as window:
            lowered = _lower_procedure(
                instance,
                intent=intent,
                base=base,
                base_tree=tree,
                accepted_reference_tree=accepted,
                candidate_identities=frozenset({query.identity.qualified})
                if candidate
                else frozenset(),
                candidate_paths=(query_path,) if candidate else (),
            )
        assert not window.unexplained
        measurements.append(
            dict(window.counts + tree.work + (accepted.work if candidate else Counter()))
        )
        lowered_bytes.append(lowered.proposed_tree["procedures/triage.json"])
    print("Procedure reference work at 10/100 unrelated Claims, Subjects, Documents:", measurements)
    assert lowered_bytes[0] == lowered_bytes[1]
    small, large = measurements
    for counter in small.keys() | large.keys():
        assert large.get(counter, 0) <= small.get(counter, 0) * RATIO + SLACK.get(counter, 0), (
            counter,
            small,
            large,
        )
