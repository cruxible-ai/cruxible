"""Name references survive target revisions and recheck only live referrers."""

from collections.abc import Mapping

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.claim_types import render_claim_type
from cruxible_client.contracts.named_references import name_query
from cruxible_client.contracts.procedures.artifacts import render_procedure
from cruxible_client.contracts.query.definitions import (
    query_definition_digest,
    render_query_definition,
)
from cruxible_core.claims.closure import (
    build_dependency_index,
    evaluate_dependency_closure,
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


def authored_query(name="definitions"):
    """Hand-written native input, without a legacy conversion round trip."""
    from cruxible_client.contracts.query.definitions import NamedQueryDefinition

    return NamedQueryDefinition.model_validate(
        {
            "identity": {"kind": "QueryDefinition", "name": name},
            "entry": {
                "tag": "playbill-query-artifacts-entry-v2",
                "artifact_kind": "Procedure",
                "selection": "all",
            },
            "result_binding": "definition",
            "result_shape": "artifact_definition",
            "result_cardinality": "many",
            "dedupe": "artifact",
            "evaluation_policy": {
                "visible_verdicts": ["supported"],
                "visible_currency": ["current"],
                "conflict_behavior": "surface_conflicts",
            },
            "default_budgets": {"max_results": 10, "max_traversal_depth": 0},
            "maximum_budgets": {"max_results": 100, "max_traversal_depth": 0},
        }
    )


def authored_capture_contract():
    from cruxible_client.contracts.captures import NamedCaptureContract

    def component(name):
        return {"target": {"kind": "Contract", "name": name}}

    return NamedCaptureContract.model_validate(
        {
            "identity": {"kind": "CaptureContract", "name": "test.capture"},
            "shape": "http_response",
            "replay": "exact",
            "components": {
                "coordinate_schemas": [component("http-response-v1")],
                "selector_schemas": [component("whole-response-v1")],
                "commitment_canonicalizer": component("canonical-json-v1"),
                "replay_policy": component("playbill.external.exact-replay-v1"),
                "provenance_rule": component("playbill.external.daemon-fetched-v1"),
                "source_subject_mapping": component("playbill.external.record-subject-v1"),
            },
            "selection_budget": {"max_bytes": 100, "max_rows": 1, "max_items": 1},
            "retention_erasure_policy": {
                "body_retention": "required_for_duration",
                "minimum_retention": {"microseconds": 1},
                "erasure": "prohibited",
                "selector_privacy": "direct_allowed",
            },
        }
    )


@pytest.mark.parametrize("named", [False, True])
def test_existing_signing_provider_is_rechecked_without_creation_gates(tmp_path, named):
    from cruxible_client.contracts.captures import capture_contract_digest, render_capture_contract
    from cruxible_client.contracts.providers import (
        NamedProviderIdentity,
        ProviderV1,
        render_provider,
    )

    instance, _ = initialize_local(tmp_path)
    current = instance.accepted_coordinate()
    contract = authored_capture_contract()
    declarations = (
        {"capture_contracts": [{"target": {"kind": "CaptureContract", "name": "test.capture"}}]}
        if named
        else {
            "capture_contract_digests": [capture_contract_digest(contract).tagged],
            "pins": [
                {
                    "role": "capture-contract",
                    "target": contract.identity,
                    "artifact_digest": capture_contract_digest(contract).tagged,
                }
            ],
        }
    )
    signer = (NamedProviderIdentity if named else ProviderV1).model_validate(
        {
            "identity": {"kind": "Provider", "name": "acme.orders"},
            "control_domain": "acme",
            "signing_keys": [
                {"key_id": "primary", "public_key": "11" * 32, "valid_from": "2026-08-16T12:00:00Z"}
            ],
            **declarations,
        }
    )
    cpath, ppath = "capture-contracts/test.capture.json", "providers/acme.orders.json"
    base = {**instance.tree_at(current.git_oid), cpath: render_capture_contract(contract)}
    parent = {**base, ppath: render_provider(signer)}
    for retired in (False, True):
        successor = contract.model_copy(
            update={
                "selection_budget": contract.selection_budget.model_copy(update={"max_bytes": 200}),
                "lifecycle": ArtifactLifecycle(
                    state="retired" if retired else "live",
                    predecessor_digest=capture_contract_digest(contract).tagged,
                ),
            }
        )
        result = evaluate_proposal_tree(
            base_tree=parent,
            current_tree=parent,
            proposed_tree={**parent, cpath: render_capture_contract(successor)},
            current=current,
            bodies=instance.body_store(),
            timestamp="2026-10-25T04:00:00.000000Z",
            rebased=False,
            actor_id="owner",
        )
        if retired:
            assert result.candidate is None
            assert any(
                "Broken referrer Provider:acme.orders" in d.message for d in result.diagnostics
            )
        else:
            assert result.diagnostics == ()
            assert result.candidate is not None
            assert result.candidate.law_evidence[0].result["referrers"] == ["Provider:acme.orders"]
        assert parent[ppath] == render_provider(signer)
    creation = evaluate_proposal_tree(
        base_tree=base,
        current_tree=base,
        proposed_tree=parent,
        current=current,
        bodies=instance.body_store(),
        timestamp="2026-10-25T04:00:00.000000Z",
        rebased=False,
        actor_id="owner",
    )
    assert creation.candidate is None
    assert any(d.code == "cruxible.provider.single_path_closed" for d in creation.diagnostics)


def authored_procedure(name="reader", query="definitions"):
    """Explicit authored fields; only compiler-owned digests are computed."""
    from cruxible_client.contracts.procedures.artifacts import (
        ProcedureArtifact,
        ProcedureOwnedContract,
    )
    from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
    from cruxible_client.contracts.procedures.models import ProcedureDefinition

    contract = ProcedureOwnedContract.model_validate(
        {
            "identity": {"kind": "Contract", "name": "io"},
            "schema": {"fields": {}, "allow_extra": True},
        }
    )

    def reference(kind, target):
        return {"target": {"kind": kind, "name": target}}

    raw = {
        "graph_format": 7,
        "name": name,
        "contract_in": reference("Contract", "io"),
        "contract_out": reference("Contract", "io"),
        "nodes": [
            {
                "kind": "state_tap",
                "node_id": "read",
                "as": "result",
                "query": reference("QueryDefinition", query),
                "parameters": {},
            }
        ]
        if query is not None
        else [
            {
                "kind": "constant",
                "node_id": "result",
                "as": "result",
                "fields": {},
                "contract_out": reference("Contract", "io"),
            }
        ],
        "returns": "result",
        "budget": {
            "wall_clock": {"microseconds": 1_000_000},
            "max_provider_calls": 0,
            "max_capture_bytes": 0,
            "max_items": 100,
        },
        "hard_caps": {
            "max_wall_clock": {"microseconds": 1_000_000},
            "max_provider_calls": 0,
            "max_capture_bytes": 0,
            "max_items": 100,
            "max_repeat_attempts": 1,
        },
        "terminal_capability": 1,
    }
    definition = ProcedureDefinition.model_validate(raw)
    return ProcedureArtifact.model_validate(
        {
            "identity": {"kind": "Procedure", "name": name},
            "definition": definition,
            "definition_digest": compute_procedure_definition_digest(definition).tagged,
            "owned_contracts": (contract,),
            "activation_policy": "snapshot",
        }
    )


def test_genesis_recovery_reproduces_all_named_law_evidence_bytes(tmp_path):
    import shutil

    from cruxible_client.contracts.canonical import canonical_bytes
    from cruxible_core.ledger.recovery import candidate_from_change_set_record
    from cruxible_core.runtime.instance import PlaybillInstance

    instance, _ = initialize_local(tmp_path)
    query = authored_query()
    procedure = authored_procedure()
    qpath, ppath = "query-definitions/definitions.json", "procedures/reader.json"
    revisions = (
        {qpath: render_query_definition(query)},
        {ppath: render_procedure(procedure)},
        {
            qpath: render_query_definition(
                query.model_copy(
                    update={
                        "description": "Compatible target revision",
                        "lifecycle": ArtifactLifecycle(
                            predecessor_digest=query_definition_digest(query).tagged
                        ),
                    }
                )
            )
        },
    )
    for n, updates in enumerate(revisions):
        parent = instance.accepted_coordinate()
        before = instance.tree_at(parent.git_oid)
        accept_tree(instance, updates, name=f"recovery-{n}")
        record = instance.accepted_history()[-1].record
        assert all("referrers" in law.result for law in record.law_evidence)
        if n == 2:
            assert record.law_evidence[0].result["referrers"] == ["Procedure:reader"]
            assert instance.tree_at(instance.accepted_coordinate().git_oid)[ppath] == before[ppath]
        expected = candidate_from_change_set_record(record)
        result = evaluate_proposal_tree(
            base_tree=before,
            current_tree=before,
            proposed_tree=instance.tree_at(instance.accepted_coordinate().git_oid),
            current=parent,
            bodies=instance.body_store(),
            timestamp=record.candidate.timestamp,
            rebased=False,
            actor_id="owner",
            replaying_accepted=True,
            historical_law_coordinates={
                member.path: (member.law_identifier, record.law_digests[member.law_identifier])
                for member in expected.members
            },
        )
        assert result.diagnostics == ()
        assert result.candidate == expected
        assert canonical_bytes(
            [e.model_dump(mode="json") for e in result.candidate.law_evidence]
        ) == (canonical_bytes([e.model_dump(mode="json") for e in record.law_evidence]))
        head = instance.accepted_coordinate()
        shutil.rmtree(PlaybillInstance._checkpoint_directory(instance.root), ignore_errors=True)
        instance = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
        assert instance.accepted_coordinate() == head


@pytest.mark.parametrize("unrelated_count", [10, 100])
def test_proposal_recheck_reads_one_bucket_per_changed_member(tmp_path, unrelated_count):
    import sys
    from dataclasses import replace

    from cruxible_core.derived.derived_rows import CanonicalRows
    from cruxible_core.proposals.proposals import build_tree_state

    instance, _ = initialize_local(tmp_path)
    current = instance.accepted_coordinate()
    query, other = authored_query(), authored_query("unrelated")
    qpath, other_path = "query-definitions/definitions.json", "query-definitions/unrelated.json"
    parent = {
        **instance.tree_at(current.git_oid),
        qpath: render_query_definition(query),
        other_path: render_query_definition(other),
        "procedures/reader.json": render_procedure(authored_procedure()),
        **{
            f"procedures/unrelated-{n}.json": render_procedure(
                authored_procedure(f"unrelated-{n}", query="unrelated")
            )
            for n in range(unrelated_count)
        },
    }
    state = build_tree_state(parent, by_name=True)
    buckets = state.dependencies.edges_by_target
    reads = []

    class CountedBuckets(CanonicalRows):
        def get(self, key, default=None):
            # Observe the actual proposal recheck loop, excluding the separate
            # incremental-index and closure-proof consumers of this same index.
            if sys._getframe(1).f_code.co_name == "_evaluate_scoped_members":
                reads.append(key)
            return super().get(key, default)

        def __iter__(self):
            raise AssertionError("Proposal rechecks must not scan unrelated referrer buckets")

    observed = CountedBuckets(buckets.rows, buckets.encode, buckets.decode)
    state = replace(state, dependencies=replace(state.dependencies, edges_by_target=observed))
    revised = query.model_copy(
        update={
            "description": "Compatible revision",
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=query_definition_digest(query).tagged
            ),
        }
    )
    result = evaluate_proposal_tree(
        base_tree=parent,
        current_tree=parent,
        proposed_tree={**parent, qpath: render_query_definition(revised)},
        current=current,
        bodies=instance.body_store(),
        parent_state=state,
        timestamp="2026-10-25T04:00:00.000000Z",
        rebased=False,
        actor_id="owner",
    )
    assert result.diagnostics == ()
    assert result.candidate is not None
    assert reads == [qpath]
    assert result.candidate.law_evidence[0].result["referrers"] == ["Procedure:reader"]


def world():
    query = name_query(
        active_work_query(
            identity=ArtifactIdentity(kind="QueryDefinition", name="claims-by-status")
        )
    )
    procedure = _artifact(_definition())
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


def test_cold_closure_evaluator_follows_names_like_served_settlement():
    """Cold and served closure evaluation must judge name-following identically.

    A current named Procedure references a Query by name, unpinned. The Query
    is revised, compatibly, outside the Procedure's own candidate scope:
    production settlement/replay (``judge_dependency_closure`` called with
    ``by_name=True``, see ``proposals.py``) treats the Procedure as a
    satisfied name-follower rather than a dependent requiring invalidation.
    The cold-from-scratch evaluator (``evaluate_dependency_closure``) must
    reach the same verdict. Before this fix, ``_closure_facts`` built its
    indexes and judged the scope with the default ``by_name=False``, which
    disagrees: it names the Procedure as a missing dependent instead of a
    satisfied name-follower, shown below by calling the same primitives the
    served path uses with ``by_name=False``.
    """

    query, _, parent = world()
    successor = query.model_copy(
        update={
            "description": "A compatible description.",
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=query_definition_digest(query).tagged
            ),
        }
    )
    candidate_tree = {**parent, QPATH: render_query_definition(successor)}
    scope = (QPATH,)

    served = judge_dependency_closure(
        parent=build_dependency_index(parent, by_name=True),
        candidate=build_dependency_index(candidate_tree, by_name=True),
        scope=scope,
        by_name=True,
    )
    cold = evaluate_dependency_closure(
        parent_tree=parent, candidate_tree=candidate_tree, scope=scope
    )

    assert served.verdict == "complete"
    assert served.missing_dependents == ()
    assert cold.verdict == served.verdict == "complete"
    assert cold.missing_dependents == served.missing_dependents == ()

    historical_served = judge_dependency_closure(
        parent=build_dependency_index(parent, by_name=True),
        candidate=build_dependency_index(candidate_tree, by_name=True),
        scope=scope,
        by_name=False,
    )
    assert historical_served.verdict == "refused"
    assert [item.path for item in historical_served.missing_dependents] == [PPATH]


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
        "triage-clock",
        line=line.identity.name,
        schedule=CadenceSchedule(interval_seconds=60),
        named=False,
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


def test_procedure_revision_requires_a_disposition_for_its_exact_line(tmp_path):
    from cruxible_client.contracts.procedures.artifacts import procedure_artifact_digest
    from cruxible_client.contracts.procedures.contract_schema import ContractSchema, PropertySchema
    from cruxible_client.contracts.procedures.line_specs import line_spec_path, render_line_spec
    from tests.test_procedures.test_line_specs import _line

    _, procedure, updates = world()
    line = _line(requested_rung=1)[0]
    line = line.model_copy(
        update={
            "procedure": line.procedure.model_copy(
                update={"artifact_digest": procedure_artifact_digest(procedure).tagged}
            ),
            "parameters": {},
        }
    )
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
    # A Line pins its exact Procedure version (rulings C2/C3): revising the
    # Procedure never re-checks the Line in place; it requires a disposition.
    assert result.candidate is None
    assert any(
        d.code == "cruxible.change_set.incomplete_closure" and path in d.message
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


def test_genesis_recovery_reproduces_subject_creation_and_key_claim_evidence(tmp_path):
    from cruxible_client.contracts.captures import (
        DirectForeignSourceSelection,
        foreign_source_capture_contract,
    )
    from cruxible_client.contracts.semantic import ContentSpan
    from cruxible_core.proposals.proposals import (
        claim_admission_accounts_from_candidate,
        claim_type_expansions_from_candidate,
    )
    from tests.core_support._claim_authoring_support import service_propose_playbill_claim
    from tests.core_support._knowledge_loop_support import (
        TIMESTAMP,
        activate,
        authoring,
        subject_shell,
    )
    from tests.core_support._support import initialize_fresh
    from tests.test_authoring.test_authoring_preflight import _seed_claim_surface

    instance, owner = initialize_fresh(tmp_path)
    _seed_claim_surface(
        instance,
        owner,
        contract=foreign_source_capture_contract("fixture.work-items"),
        additional_subjects=(subject_shell("wi-43"),),
    )

    def differences(a, b, path="$"):
        if isinstance(a, dict) and isinstance(b, dict):
            return [
                d
                for key in a.keys() | b.keys()
                for d in differences(a.get(key), b.get(key), path + "." + key)
            ]
        if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
            return [
                d
                for index, (x, y) in enumerate(zip(a, b, strict=True))
                for d in differences(x, y, f"{path}[{index}]")
            ]
        return [] if a == b else [(path, a, b)]

    parent = instance.accepted_coordinate()
    before = instance.tree_at(parent.git_oid)
    from cruxible_core.indexes.evaluated_state import derive_indexed_state
    from cruxible_core.proposals.proposals import build_tree_state

    warm = derive_indexed_state(instance.immutable_tree_at(parent.git_oid))
    cold = build_tree_state(before, by_name=True)
    warm_edges = [edge.model_dump(mode="json") for edge in warm.dependencies.edges()]
    cold_edges = [edge.model_dump(mode="json") for edge in cold.dependencies.edges()]
    assert warm_edges == cold_edges
    assert warm.dependencies.edge_root == cold.dependencies.edge_root
    body = instance.body_store().store(b"status: ready")
    inspection = service_propose_playbill_claim(
        instance,
        authoring=authoring("wi-42", "ready", with_claim_type=False).model_copy(
            update={
                "source_selection": DirectForeignSourceSelection(
                    logical_source_identity="fixture.work-items",
                    span=ContentSpan(content_digest=body.digest, start_byte=0, end_byte=13),
                ),
            }
        ),
        actor_id="owner",
        proposal_name="ordinary-claim",
        timestamp=TIMESTAMP,
    )
    proposed = inspection.proposal.proposal
    candidate = proposed.candidate
    assert candidate is not None, proposed.evaluation.diagnostics
    replay = evaluate_proposal_tree(
        base_tree=before,
        current_tree=before,
        proposed_tree=instance.proposal_tree(proposed.evaluation.evaluated_tree_oid),
        current=parent,
        bodies=instance.body_store(),
        timestamp=candidate.candidate.timestamp,
        rebased=False,
        actor_id="owner",
        replaying_accepted=True,
        replay_claim_admission_accounts=claim_admission_accounts_from_candidate(candidate),
        claim_type_expansions=claim_type_expansions_from_candidate(candidate),
        historical_law_coordinates={
            member.path: (member.law_identifier, candidate.law_digests[member.law_identifier])
            for member in candidate.members
        },
    )
    assert replay.diagnostics == ()
    assert replay.candidate is not None
    assert replay.candidate == candidate, differences(
        candidate.model_dump(mode="json"), replay.candidate.model_dump(mode="json")
    )

    activate(instance, owner, inspection)


def test_ordinary_claim_fixture_survives_genesis_recovery(tmp_path):
    from cruxible_core.runtime.instance import PlaybillInstance
    from tests.core_support._knowledge_loop_support import seed_claims

    instance, _ = seed_claims(tmp_path)
    current = instance.accepted_coordinate()
    cold = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert cold.accepted_coordinate() == current
