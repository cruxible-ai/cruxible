"""Procedure spec lowering reads its references, independently of unrelated state."""

from collections import Counter
from collections.abc import Iterator

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.documents import render_document
from cruxible_client.contracts.procedures.authored import ReadStep, ReturnFields
from cruxible_client.contracts.query.definitions import (
    query_definition_path,
    render_query_definition,
)
from cruxible_client.contracts.subjects import render_subject, subject_path
from cruxible_core.authoring.lowering import _lower_procedure
from cruxible_core.derived.derived_state import SnapshotTree
from tests.support.procedures import procedure_spec_data, ref, spec_payload
from tests.test_authoring.test_authoring_history_reuse import _intent
from tests.test_authoring.test_authoring_procedures import (
    LIMITS,
    ROWS,
    _change_set_query,
    _world_holding_a_document,
)
from tests.test_claims.test_claims import _claim, _subject
from tests.test_scale._counters import counting, trace_sqlite
from tests.test_scale.test_scale_guard import RATIO, SLACK


@pytest.fixture(autouse=True)
def traced_sqlite(tmp_path):
    with trace_sqlite(tmp_path):
        yield


def _triage_definition(*, query: str | None = None) -> dict[str, object]:
    """The triage Procedure, authored with only template-open step kinds.

    ``tests.test_authoring.test_authoring_procedures._triage_definition`` uses
    a ``project`` step: that module is marked ``executor_only`` and suspends
    the template refusal (D-R37 h1) to cover the step's executor handler.
    Lowering here goes through the ordinary authoring path
    (``_lower_procedure`` -> ``resolve_procedure_spec``), which enforces that
    refusal, so a ``project`` step would be refused as
    ``procedure_step_closed``. This spells the same Procedure (the spec names
    its Query; the run's own value answers "complete") with the template's
    own return form (``returns: {fields: ...}``) instead of an intermediate
    ``project`` step, proving the same thing: resolving the named Query is
    flat, independent of unrelated state.
    """

    steps: list[dict[str, object]] = []
    if query is not None:
        steps.append(ReadStep(read="rows", id="read", query=query))
    return procedure_spec_data(
        procedure="triage",
        description="Read accepted claims and shape a bounded result.",
        input={},
        output=ROWS,
        activation="drain",
        steps=steps,
        returns=ReturnFields(
            fields={"complete": ref("$steps.rows.completed") if query is not None else True},
            id="result",
        ),
        hard_caps=LIMITS,
    )


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


@pytest.mark.parametrize("candidate", [True])
def test_spec_procedure_reference_work_is_flat_at_ten_times_unrelated_state(tmp_path, candidate):
    """The spec names its Query; lowering resolves that one name, never a scan."""

    from cruxible_client.contracts.claims import claim_path, render_claim
    from cruxible_client.contracts.documents import parse_document

    coordinator, _actor = _world_holding_a_document(tmp_path)
    instance = coordinator.instance
    base = instance.accepted_coordinate()
    original = dict(instance.immutable_tree_at(base.git_oid))
    document = parse_document(original["documents/design.json"], path="documents/design.json")
    query = _change_set_query()
    query_path = query_definition_path(query.identity.name)
    payload = spec_payload(_triage_definition(query=query.identity.name))
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
        with counting(instance.root) as window:
            lowered = _lower_procedure(
                instance,
                intent=intent,
                base=base,
                base_tree=tree,
                candidate_paths=(query_path,),
            )
        assert not window.unexplained
        measurements.append(dict(window.counts + tree.work))
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
