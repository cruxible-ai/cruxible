"""Name references survive target revisions and recheck only live referrers."""

from collections.abc import Mapping

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.claim_types import render_claim_type
from cruxible_client.contracts.named_references import name_query
from cruxible_client.contracts.procedures.artifacts import render_procedure
from cruxible_client.contracts.procedures.names import name_procedure
from cruxible_client.contracts.query.definitions import (
    query_definition_digest,
    render_query_definition,
)
from cruxible_core.claims.closure import (
    build_dependency_index,
    judge_dependency_closure,
    update_dependency_index,
)
from cruxible_core.proposals.proposals import evaluate_proposal_tree
from tests.core_support._identity_support import accept_tree
from tests.core_support._support import initialize_local
from tests.test_procedures.test_procedure_artifacts import _artifact, _definition
from tests.test_query.test_query_definitions import (
    REVIEWER_PREDICATE,
    STATUS_PREDICATE,
    active_work_query,
    claim_type,
)

QPATH = "query-definitions/claims-by-status.json"
PPATH = "procedures/triage.json"


def world():
    query = name_query(
        active_work_query(
            identity=ArtifactIdentity(kind="QueryDefinition", name="claims-by-status")
        )
    )
    procedure = name_procedure(_artifact(_definition()))
    return (
        query,
        procedure,
        {
            QPATH: render_query_definition(query),
            PPATH: render_procedure(procedure),
            "claim-types/project.work_item/status.json": render_claim_type(
                claim_type(STATUS_PREDICATE)
            ),
            "claim-types/project.work_item/reviewed_by.json": render_claim_type(
                claim_type(REVIEWER_PREDICATE, object_kind="subject")
            ),
        },
    )


def test_reverse_index_survives_a_target_revision_without_repinning():
    query, _, parent = world()
    successor = query.model_copy(
        update={
            "description": "A compatible description.",
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=query_definition_digest(query).tagged
            ),
        }
    )
    candidate = {**parent, QPATH: render_query_definition(successor)}
    before = build_dependency_index(parent)
    after = update_dependency_index(before, tree=candidate, changed=(QPATH,))
    assert after.edges() == build_dependency_index(candidate).edges()
    assert [e.source_path for e in after.edges_by_target[QPATH]] == [PPATH]
    result = judge_dependency_closure(parent=before, candidate=after, scope=(QPATH,), by_name=True)
    assert result.verdict == "complete"
    assert result.missing_dependents == ()
    assert candidate[PPATH] == parent[PPATH]


def test_proposal_lists_compatible_referrers_and_names_broken_ones(tmp_path):
    query, _, updates = world()
    instance, _ = initialize_local(tmp_path)
    accept_tree(instance, updates, name="named-reference-world")
    current = instance.accepted_coordinate()
    parent = instance.tree_at(current.git_oid)

    def evaluate(retired):
        successor = query.model_copy(
            update={
                "description": "Revised query.",
                "lifecycle": ArtifactLifecycle(
                    state="retired" if retired else "live",
                    predecessor_digest=query_definition_digest(query).tagged,
                ),
            }
        )
        return evaluate_proposal_tree(
            base_tree=parent,
            current_tree=parent,
            proposed_tree={**parent, QPATH: render_query_definition(successor)},
            current=current,
            bodies=instance.body_store(),
            timestamp="2026-10-25T04:00:00.000000Z",
            rebased=False,
            actor_id="owner",
        )

    compatible = evaluate(False)
    assert compatible.diagnostics == ()
    assert compatible.candidate is not None
    assert compatible.candidate.candidate.scope == (QPATH,)
    law = next(e for e in compatible.candidate.law_evidence if e.path == QPATH)
    assert law.result["referrers"] == ["Procedure:triage"]
    broken = evaluate(True)
    assert broken.candidate is None
    assert any("Broken referrer Procedure:triage" in d.message for d in broken.diagnostics)


def test_legacy_dependency_edges_keep_exact_versions_before_compiler_36():
    from cruxible_client.contracts.dependencies import dependency_pins
    from cruxible_client.contracts.procedures.line_specs import line_spec_path, render_line_spec
    from cruxible_client.contracts.triggers import CadenceSchedule, render_trigger, trigger_path
    from tests.support.lines import line_trigger
    from tests.test_procedures.test_line_specs import _line

    line, _, _ = _line()
    trigger = line_trigger(
        "triage-clock", line=line.identity.name, schedule=CadenceSchedule(interval_seconds=60)
    )
    # Frozen Trigger v1 omits the target Line from its pins. Its old semantic
    # root must keep that omission; compiler 36 indexes the explicit name.
    assert dependency_pins(trigger, by_name=False) == trigger.pins
    assert not any(p.target == line.identity for p in trigger.pins)
    assert any(p.target == line.identity for p in dependency_pins(trigger, by_name=True))
    tree = {
        line_spec_path(line.identity.name): render_line_spec(line),
        trigger_path(trigger.identity.name): render_trigger(trigger),
    }
    frozen = build_dependency_index(tree, by_name=False)
    current = build_dependency_index(tree, by_name=True)
    assert not frozen.edges_by_target.get(line_spec_path(line.identity.name))
    assert [e.source_path for e in current.edges_by_target[line_spec_path(line.identity.name)]] == [
        trigger_path(trigger.identity.name)
    ]


def test_old_procedure_reference_stays_exact_only_in_the_frozen_index():
    from cruxible_client.contracts.artifacts import ArtifactPin

    _, _, tree = world()
    old = _artifact(_definition())
    tree[PPATH] = render_procedure(old)
    # The query exists at this identity, but differs from the old authored pin.
    assert all(p.artifact_digest != query_definition_digest(world()[0]).tagged for p in old.pins)
    frozen = build_dependency_index(tree, by_name=False)
    current = build_dependency_index(tree, by_name=True)
    assert all(isinstance(p, ArtifactPin) for p in frozen.states[PPATH].pins)
    assert not frozen.edges_by_target.get(QPATH)
    assert [e.source_path for e in current.edges_by_target[QPATH]] == [PPATH]


def test_procedure_revision_rechecks_unchanged_line_parameters(tmp_path):
    from cruxible_client.contracts.named_references import name_line
    from cruxible_client.contracts.procedures.artifacts import procedure_artifact_digest
    from cruxible_client.contracts.procedures.contract_schema import ContractSchema, PropertySchema
    from cruxible_client.contracts.procedures.line_specs import line_spec_path, render_line_spec
    from tests.test_procedures.test_line_specs import _line

    _, procedure, updates = world()
    line = name_line(_line(requested_rung=1)[0]).model_copy(update={"parameters": {}})
    path = line_spec_path(line.identity.name)
    instance, _ = initialize_local(tmp_path)
    accept_tree(instance, {**updates, path: render_line_spec(line)}, name="named-line-world")
    current = instance.accepted_coordinate()
    parent = instance.tree_at(current.git_oid)
    contracts = tuple(
        c.model_copy(
            update={
                "contract_schema": ContractSchema(
                    fields={"required": PropertySchema(type="string")}, allow_extra=False
                )
            }
        )
        if c.identity == procedure.definition.contract_in.target
        else c
        for c in procedure.owned_contracts
    )
    successor = procedure.model_copy(
        update={
            "owned_contracts": contracts,
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=procedure_artifact_digest(procedure).tagged
            ),
        }
    )
    result = evaluate_proposal_tree(
        base_tree=parent,
        current_tree=parent,
        proposed_tree={**parent, PPATH: render_procedure(successor)},
        current=current,
        bodies=instance.body_store(),
        timestamp="2026-10-25T04:00:00.000000Z",
        rebased=False,
        actor_id="owner",
    )
    assert result.candidate is None
    assert any(
        d.code == "cruxible.line.parameters_invalid"
        and "Broken referrer Line:triage-hourly" in d.message
        for d in result.diagnostics
    ), result.diagnostics
    assert parent[path] == render_line_spec(line)


@pytest.mark.parametrize("unrelated_count", [10, 100])
def test_name_index_update_reads_only_changed_bytes_at_two_instance_sizes(unrelated_count):
    query, _, parent = world()
    for n in range(unrelated_count):
        other = query.model_copy(
            update={"identity": ArtifactIdentity(kind="QueryDefinition", name=f"unrelated-{n}")}
        )
        parent[f"query-definitions/unrelated-{n}.json"] = render_query_definition(other)
    index = build_dependency_index(parent, by_name=True)
    successor = query.model_copy(
        update={
            "description": "Compatible revision",
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=query_definition_digest(query).tagged
            ),
        }
    )
    candidate = {**parent, QPATH: render_query_definition(successor)}
    reads = []

    class ChangedBytes(Mapping):
        def __getitem__(self, key):
            reads.append(key)
            return candidate[key]

        def __iter__(self):
            raise AssertionError("A warm dependency update must not scan the tree")

        def __len__(self):
            raise AssertionError("A warm dependency update must not count the tree")

    after = update_dependency_index(index, tree=ChangedBytes(), changed=(QPATH,))
    assert reads == [QPATH]
    assert [e.source_path for e in after.edges_by_target[QPATH]] == [PPATH]
    assert after.edge_root == build_dependency_index(candidate, by_name=True).edge_root


def test_named_referrer_evidence_and_compact_rechecks_survive_cold_recovery(tmp_path):
    from cruxible_core.runtime.instance import PlaybillInstance

    query, _, updates = world()
    instance, _ = initialize_local(tmp_path)
    accept_tree(instance, updates, name="named-replay-world")
    procedure_bytes = instance.tree_at(instance.accepted_coordinate().git_oid)[PPATH]
    successor = query.model_copy(
        update={
            "description": "A compatible revision that keeps its named reader.",
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=query_definition_digest(query).tagged
            ),
        }
    )
    accept_tree(instance, {QPATH: render_query_definition(successor)}, name="named-replay-revision")
    coordinate = instance.accepted_coordinate()
    instance.refresh()
    assert instance.accepted_coordinate() == coordinate
    cold = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert cold.accepted_coordinate() == coordinate
    assert cold.tree_at(coordinate.git_oid)[PPATH] == procedure_bytes
