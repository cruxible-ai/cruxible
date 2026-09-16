"""Definition queries retain dynamic membership and exact historical meaning."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.authoring.inputs import QueryDefinitionInput, lower_authoring_input
from cruxible_client.contracts.authoring.models import (
    ChangeSetAuthoringPayloadV1,
    ClaimTypeAuthoringPayloadV1,
)
from cruxible_client.contracts.claim_types import (
    claim_type_digest,
    claim_type_path,
    render_claim_type,
)
from cruxible_client.contracts.declared_blocks import projection_query_semantic_result_digest
from cruxible_client.contracts.query.definitions import (
    QueryDefinitionSpecV1,
    QueryDefinitionV1,
    parse_query_definition,
    query_definition_digest,
    query_definition_path,
    render_query_definition,
)
from cruxible_client.contracts.query.grammar import QueryArtifactsEntryV2, QueryBudgetsV1
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.indexes.typed_state import TypedStateReader
from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.query.engine import claim_query_result_digest
from cruxible_core.service.discovery.query import service_run_playbill_query
from tests.core_support._knowledge_loop_support import TIMESTAMP, accept_proposal, work_item_query
from tests.core_support._support import initialize_local
from tests.test_authoring.test_authoring_change_set_intents import _predicate_type

NOW = datetime(2026, 9, 16, tzinfo=UTC)


def definition(
    kind="ClaimType", *, namespaces=(), prefixes=(), name="security.ontology", max_results=100
):
    return QueryDefinitionV1(
        artifact_format="playbill-query-definition-v2",
        identity=ArtifactIdentity(kind="QueryDefinition", name=name),
        entry=QueryArtifactsEntryV2(
            artifact_kind=kind,
            selection="namespaces" if namespaces else "name_prefixes" if prefixes else "all",
            namespaces=namespaces,
            name_prefixes=prefixes,
        ),
        result_binding="definition",
        result_shape="artifact_definition",
        result_cardinality="many",
        dedupe="artifact",
        evaluation_policy=work_item_query().evaluation_policy,
        default_budgets=QueryBudgetsV1(max_results=max_results, max_traversal_depth=0),
        maximum_budgets=QueryBudgetsV1(max_results=max_results, max_traversal_depth=0),
    )


def accept(instance, owner, *artifacts):
    tree = dict(instance.tree_at(instance.accepted_coordinate().git_oid))
    for item in artifacts:
        if isinstance(item, QueryDefinitionV1):
            tree[query_definition_path(item.identity.name)] = render_query_definition(item)
        else:
            tree[claim_type_path(item.predicate)] = render_claim_type(item)
    result = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/owner/ontology-{len(instance.accepted_history())}",
            proposed_base_oid=instance.accepted_coordinate().git_oid,
        ),
        candidate_tree=tree,
        timestamp=TIMESTAMP,
    )
    accept_proposal(instance, owner, SimpleNamespace(proposal=result))


def run(instance, query, **kwargs):
    return service_run_playbill_query(
        instance, name=query.identity.name, evaluation_time=NOW, **kwargs
    )


@pytest.mark.parametrize(
    "entry",
    [
        dict(artifact_kind="ClaimType", selection="namespaces"),
        dict(artifact_kind="ClaimType", selection="all", namespaces=("security",)),
        dict(artifact_kind="ClaimType", selection="namespaces", namespaces=("sec", "sec")),
        dict(artifact_kind="ClaimType", selection="namespaces", namespaces=("sec%",)),
        dict(artifact_kind="Procedure", selection="namespaces", namespaces=("sec",)),
        dict(artifact_kind="Procedure", selection="name_prefixes", name_prefixes=("sec",)),
        dict(artifact_kind="ClaimType", selection="name_prefixes", name_prefixes=("sec.",)),
    ],
)
def test_invalid_scope_is_not_a_broad_read(entry):
    with pytest.raises(ValidationError):
        QueryArtifactsEntryV2(**entry)


def test_v1_definition_and_result_encoding_stay_frozen():
    old = work_item_query()
    assert (
        parse_query_definition(
            render_query_definition(old), path=query_definition_path(old.identity.name)
        )
        == old
    )
    bad = definition().model_dump(mode="json")
    bad["artifact_format"] = "playbill-query-definition-v1"
    with pytest.raises(ValidationError):
        QueryDefinitionV1.model_validate(bad)


def test_scope_membership_versions_history_and_empty_result(tmp_path, monkeypatch):
    instance, owner = initialize_local(tmp_path)
    scoped = definition(namespaces=("security.asset",))
    all_types = definition(name="security.all")
    multiple = definition(namespaces=("other", "security.asset"), name="security.multiple")
    accept(instance, owner, scoped, all_types, multiple)
    empty = run(instance, scoped)
    assert empty.result.rows == ()
    origin = empty.coordinate
    selected = _predicate_type("security.asset.owner")
    sibling = _predicate_type("security.asset.installs")
    excluded = _predicate_type("security.asset.deep.value")
    other = _predicate_type("other.external")
    accept(instance, owner, selected, sibling, excluded, other)
    current = run(instance, scoped)
    assert [r.artifact.identity for r in current.result.rows] == [
        "ClaimType:security.asset.installs",
        "ClaimType:security.asset.owner",
    ]
    assert len(run(instance, all_types).result.rows) == 4
    assert len(run(instance, multiple).result.rows) == 3
    assert projection_query_semantic_result_digest(
        current.result.model_dump(mode="json")
    ) != projection_query_semantic_result_digest(empty.result.model_dump(mode="json"))
    assert run(instance, scoped, at=origin).result == empty.result
    assert current.receipt.result_digest == claim_query_result_digest(current.result)
    before = projection_query_semantic_result_digest(current.result.model_dump(mode="json"))
    accept(instance, owner, _predicate_type("outside.unrelated"))
    assert (
        projection_query_semantic_result_digest(
            run(instance, scoped).result.model_dump(mode="json")
        )
        == before
    )
    changed = selected.model_copy(
        update={
            "literal_schema": {"enum": ["blocked", "done", "ready", "waiting"], "type": "string"},
            "lifecycle": ArtifactLifecycle(predecessor_digest=claim_type_digest(selected).tagged),
        }
    )
    accept(instance, owner, changed)
    assert (
        projection_query_semantic_result_digest(
            run(instance, scoped).result.model_dump(mode="json")
        )
        != before
    )
    retired = changed.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired", predecessor_digest=claim_type_digest(changed).tagged
            )
        }
    )
    accept(instance, owner, retired)
    assert len(run(instance, scoped).result.rows) == 1
    # A warm read opens only the selected definition, not every kind or the tree.
    reads = []
    original = TypedStateReader.member_bytes

    def read(reader, path):
        reads.append(path)
        return original(reader, path)

    def forbidden(*args, **kwargs):
        pytest.fail("artifact query must not rebuild Claim facts or scan accepted tree")

    monkeypatch.setattr(TypedStateReader, "member_bytes", read)
    monkeypatch.setattr(instance, "tree_at", forbidden)
    monkeypatch.setattr(instance, "immutable_tree_at", forbidden)
    run(instance, scoped)
    assert set(reads) == {
        query_definition_path(scoped.identity.name),
        claim_type_path(sibling.predicate),
    }


def test_limits_and_bad_parameters_are_explicit(tmp_path):
    instance, owner = initialize_local(tmp_path)
    query = definition(max_results=1)
    accept(instance, owner, query, _predicate_type("security.a"), _predicate_type("security.b"))
    result = run(instance, query).result
    assert len(result.rows) == 1 and result.truncation.candidate_result_count == 2
    assert result.truncation.clipped_budgets == ("max_results",)
    assert run(instance, query, parameters={"unknown": True}).result.verdict == "refused"
    assert (
        run(
            instance, query, budgets=QueryBudgetsV1(max_results=2, max_traversal_depth=0)
        ).result.verdict
        == "refused"
    )


def test_changeset_resolves_query_vocabulary_without_manual_pins(tmp_path):
    instance, _ = initialize_local(tmp_path)
    payload = work_item_query().model_dump(mode="json")
    payload["pins"] = []
    draft = QueryDefinitionInput(
        kind="query_definition", query_definition=QueryDefinitionSpecV1.model_validate(payload)
    )
    members = (
        ClaimTypeAuthoringPayloadV1(claim_type=_predicate_type("project.work_item.status")),
        lower_authoring_input(draft),
    )
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id="owner")
    intent = coordinator.create(
        actor=actor,
        canonical_timestamp=TIMESTAMP,
        payload=ChangeSetAuthoringPayloadV1(members=members),
    ).intent
    result = coordinator.preflight(intent.intent_id, actor=actor)
    assert result.verdict == "passed", result.frontier


def accept_input(instance, owner, input):
    from cruxible_core.authoring.store import AuthoringIntentStore
    from cruxible_core.service.authoring.documents import (
        service_activate_playbill_proposal,
        service_submit_playbill_approval,
    )
    from tests.test_authoring.test_authoring_change_set_intents import TIMESTAMP as AUTHOR_TIME
    from tests.test_ledger.test_activation import _sign

    coordinator = AuthoringIntentCoordinator(
        instance=instance,
        store=AuthoringIntentStore(instance.root / instance.descriptor.storage.exhaust),
        clock=lambda: datetime(2026, 8, 22, 12, tzinfo=UTC),
    )
    actor = AuthenticatedActor(actor_id="owner")
    intent = coordinator.create_input(
        actor=actor, input=input, canonical_timestamp=AUTHOR_TIME
    ).intent
    result = coordinator.submit(intent.intent_id, actor=actor)
    assert result.status.candidate_digest, result
    approval = _sign(
        owner, result.status.candidate_digest, instance.accepted_coordinate().semantic_root
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=result.status.proposal_id,
        attestation=approval.attestation,
        authenticated_submitter="owner",
    )
    assert (
        service_activate_playbill_proposal(
            instance, proposal_id=result.status.proposal_id, activated_by="owner"
        ).status
        == "accepted"
    )


def test_procedure_selection_retains_versions_and_uses_name_scope(tmp_path):
    from cruxible_client.authoring.examples import procedure_example
    from cruxible_client.contracts.procedures.artifacts import (
        procedure_artifact_digest,
        procedure_path,
        render_procedure,
    )

    instance, owner = initialize_local(tmp_path)
    query = definition("Procedure", prefixes=("security.",))
    all_procedures = definition("Procedure", name="security.all_procedures")
    accept(instance, owner, query, all_procedures)
    assert run(instance, query).result.rows == ()

    def procedure(name):
        example = procedure_example()
        return example.model_copy(update={"definition": {**example.definition, "name": name}})

    accept_input(instance, owner, procedure("security.observe"))
    original = run(instance, query)
    assert [row.artifact.identity for row in original.result.rows] == ["Procedure:security.observe"]
    assert original.result.rows[0].artifact.definition.definition.name == "security.observe"
    before = projection_query_semantic_result_digest(original.result)
    accept_input(instance, owner, procedure("security_other.observe"))
    assert len(run(instance, all_procedures).result.rows) == 2
    assert projection_query_semantic_result_digest(run(instance, query).result) == before
    old = original.result.rows[0].artifact.definition
    revised = old.model_copy(
        update={
            "activation_policy": "drain",
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=procedure_artifact_digest(old).tagged
            ),
        }
    )
    tree = dict(instance.tree_at(instance.accepted_coordinate().git_oid))
    tree[procedure_path(old.identity.name)] = render_procedure(revised)
    from tests.test_indexes.test_resolution_contracts import _accept_tree

    _accept_tree(instance, owner, tree, timestamp=TIMESTAMP, proposal_name="procedure-revision")
    assert projection_query_semantic_result_digest(run(instance, query).result) != before
    assert run(instance, query, at=original.coordinate).result == original.result
    retired = revised.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired", predecessor_digest=procedure_artifact_digest(revised).tagged
            )
        }
    )
    tree = dict(instance.tree_at(instance.accepted_coordinate().git_oid))
    tree[procedure_path(old.identity.name)] = render_procedure(retired)
    _accept_tree(instance, owner, tree, timestamp=TIMESTAMP, proposal_name="procedure-retired")
    assert run(instance, query).result.rows == ()


@pytest.mark.parametrize("policy,severity", [("warn", "warning"), ("require_current", "blocking")])
def test_ontology_query_only_blocks_share_sync_and_next_currency(tmp_path, policy, severity):
    from cruxible_client.contracts.authoring.models import PlaybillProjectionCheckRequestV1
    from cruxible_client.contracts.declared_blocks import (
        ProjectionBlockStampV2,
        ProjectionQueryBackingV1,
        projection_parameter_digest,
    )
    from cruxible_client.contracts.projection import AcceptedCoordinate
    from cruxible_core.service.authoring.projection_sync import service_check_projection_blocks
    from tests.test_indexes.test_projection_next import _projection_rows, _request

    instance, owner = initialize_local(tmp_path)
    query = definition(namespaces=("security",), max_results=1)
    accept(instance, owner, query)
    initial = run(instance, query)
    backing = ProjectionQueryBackingV1(
        identity=query.identity,
        definition_digest=query_definition_digest(query).tagged,
        resolved_parameter_bindings=(),
        canonical_param_digest=projection_parameter_digest(()),
        declared_evaluation_time=NOW,
        semantic_result_digest=projection_query_semantic_result_digest(initial.result),
    )
    request = _request(instance, backing=(backing,), evaluation_time=NOW)
    observed = request.workspace_observation.source_observations[0]
    marker = observed.marker_summaries[0]
    stamp = ProjectionBlockStampV2.model_validate(
        {
            **marker.stamp.model_dump(mode="json"),
            "tag": "playbill-projection-stamp-v2",
            "currency_policy": policy,
        }
    )

    def checks():
        at = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
        check = service_check_projection_blocks(
            instance,
            request=PlaybillProjectionCheckRequestV1(stamps=(stamp,), at=at, evaluation_time=NOW),
        ).results[0]
        current_request = request.model_copy(
            update={
                "at": at,
                "workspace_observation": request.workspace_observation.model_copy(
                    update={
                        "source_observations": (
                            observed.model_copy(
                                update={
                                    "marker_summaries": (
                                        marker.model_copy(update={"stamp": stamp}),
                                    )
                                }
                            ),
                        )
                    }
                ),
            }
        )
        return check, _projection_rows(instance, current_request)

    check, rows = checks()
    assert check.status == "current" and rows == ()
    accept(instance, owner, _predicate_type("security.first"))
    check, rows = checks()
    assert check.status == "successor"
    assert [row.severity for row in rows] == [severity]
    accept(instance, owner, _predicate_type("security.second"))
    check, rows = checks()
    assert check.status == "unchecked"
    assert rows[0].detail["backing_state"] == "unchecked"


def test_old_compiler_cannot_reinterpret_artifact_query_format():
    from cruxible_client.contracts.errors import ProjectionFormatError
    from cruxible_core.compiler.compiler import (
        ONTOLOGY_COMPILER,
        RESOLUTION_COMPILER,
        artifact_kinds_for_compiler,
        projection_registry_for_compiler,
    )
    from cruxible_core.compiler.projection_artifacts import parse_projection_tree

    query = definition()
    tree = {query_definition_path(query.identity.name): render_query_definition(query)}
    with pytest.raises(ProjectionFormatError):
        parse_projection_tree(
            tree,
            registry=projection_registry_for_compiler(RESOLUTION_COMPILER),
            artifact_kinds=artifact_kinds_for_compiler(RESOLUTION_COMPILER),
        )
    projected = parse_projection_tree(
        tree,
        registry=projection_registry_for_compiler(ONTOLOGY_COMPILER),
        artifact_kinds=artifact_kinds_for_compiler(ONTOLOGY_COMPILER),
    )
    assert projected.envelopes[0].artifact_digest == query_definition_digest(query).tagged


@pytest.mark.parametrize("failure", ["absent", "retired", "explicit_mismatch"])
def test_query_pin_resolution_refuses_missing_or_different_vocabulary(tmp_path, failure):
    instance, owner = initialize_local(tmp_path)
    claim_type = _predicate_type("project.work_item.status")
    if failure != "absent":
        accept(instance, owner, claim_type)
    if failure == "retired":
        accept(
            instance,
            owner,
            claim_type.model_copy(
                update={
                    "lifecycle": ArtifactLifecycle(
                        state="retired", predecessor_digest=claim_type_digest(claim_type).tagged
                    )
                }
            ),
        )
    body = work_item_query().model_dump(mode="json")
    if failure == "explicit_mismatch":
        body["pins"][0]["artifact_digest"] = "sha256:" + "0" * 64
    else:
        body["pins"] = []
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id="owner")
    intent = coordinator.create_input(
        actor=actor,
        input=QueryDefinitionInput(
            kind="query_definition", query_definition=QueryDefinitionSpecV1.model_validate(body)
        ),
        canonical_timestamp=TIMESTAMP,
    ).intent
    result = coordinator.preflight(intent.intent_id, actor=actor)
    assert result.verdict == "refused"
    assert result.frontier.diagnostics[0].code == (
        "playbill.authoring.query_pin_mismatch"
        if failure == "explicit_mismatch"
        else "playbill.authoring.claim_type_missing"
    )
