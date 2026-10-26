"""Kit planning leaves compatible name references and explicitly stops retire-with."""

import pytest

from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.named_references import name_line, name_trigger
from cruxible_client.contracts.procedures.artifacts import (
    procedure_artifact_digest,
    render_procedure,
)
from cruxible_client.contracts.procedures.line_specs import line_spec_path, render_line_spec
from cruxible_client.contracts.procedures.models import ConstantNode
from cruxible_client.contracts.procedures.names import name_procedure
from cruxible_client.contracts.triggers import CadenceSchedule, render_trigger, trigger_path
from cruxible_core.providers.build_moves import settle_changes
from cruxible_core.service.kits import _planned_automation
from tests.core_support._identity_support import accept_tree
from tests.core_support._support import initialize_local
from tests.support.lines import line_trigger
from tests.test_procedures.test_line_specs import _line
from tests.test_procedures.test_procedure_artifacts import _artifact, _definition


@pytest.mark.parametrize(
    "family", ["procedure", "blueprint", "query", "capture", "policy", "provider", "signer"]
)
def test_retiring_legacy_definitions_on_36_uses_admitted_successor_formats(tmp_path, family):
    from cruxible_client.contracts.acquisition_policies import (
        SourceAcquisitionPolicyV1,
        render_acquisition_policy,
    )
    from cruxible_client.contracts.canonical import pretty_canonical_bytes
    from cruxible_client.contracts.captures import (
        CaptureContract,
        CaptureRetentionErasurePolicy,
        CaptureSelectionBudget,
        capture_shape_components,
        render_capture_contract,
    )
    from cruxible_client.contracts.procedures.blueprints import BlueprintArtifact, render_blueprint
    from cruxible_client.contracts.providers import ProviderV1, provider_path, render_provider
    from cruxible_client.contracts.query.definitions import QueryDefinition, render_query_definition
    from cruxible_core.claims.closure import parse_dependency_artifact
    from cruxible_core.proposals.proposals import evaluate_proposal_tree
    from cruxible_core.service.kits import _retirement
    from cruxible_core.service.procedures.provider_builds import _retired_provider
    from tests.core_support._p2b1_support import accepted_interface
    from tests.test_procedures.test_named_reference_closure import authored_procedure
    from tests.test_providers.test_provider_builds import build

    procedure = authored_procedure(name="old", query=None, named=False)
    instance, _ = initialize_local(tmp_path)
    current = instance.accepted_coordinate()
    parent = instance.tree_at(current.git_oid)
    if family == "procedure":
        path, content, tag = (
            "procedures/old.json",
            render_procedure(procedure),
            "cruxible-procedure-v3",
        )
    elif family == "blueprint":
        from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
        from cruxible_client.contracts.procedures.models import ProcedureDefinition
        from cruxible_client.contracts.provider_interfaces import (
            ProviderInterfaceContract,
            interface_definition_digest,
            provider_interface_digest,
            render_provider_interface,
        )

        interface = ProviderInterfaceContract.model_validate(
            {
                "identity": {"kind": "ProviderInterface", "name": "test.lookup/v1"},
                "interface": {
                    "interface_id": "test.lookup",
                    "version": 1,
                    "effect_class": "pure",
                    "contracts": {
                        "input": {"fields": {}, "allow_extra": True},
                        "output": {"fields": {}, "allow_extra": True},
                    },
                },
            }
        )
        parent["provider-interfaces/test.lookup/v1.json"] = render_provider_interface(interface)
        pin = {
            "role": "provider-interface",
            "target": interface.identity.model_dump(),
            "artifact_digest": provider_interface_digest(interface).tagged,
        }
        raw = procedure.definition.model_dump(mode="json", by_alias=True)
        raw["nodes"] = [
            {
                "kind": "call",
                "node_id": "lookup",
                "as": "result",
                "input": {},
                "provider": {"tag": "playbill-procedure-pin-slot-ref-v1", "slot_name": "lookup"},
                "interface": pin,
                "interface_digest": interface_definition_digest(interface),
            }
        ]
        raw["pin_slots"] = [
            {
                "slot_name": "lookup",
                "pin_role": "provider",
                "artifact_kind": "Provider",
                "interface_digest": interface_definition_digest(interface),
            }
        ]
        raw["budget"]["max_provider_calls"] = raw["hard_caps"]["max_provider_calls"] = 1
        definition = ProcedureDefinition.model_validate(raw)
        blueprint = BlueprintArtifact.model_validate(
            {
                **procedure.model_dump(
                    exclude={"artifact_format", "identity", "acquisition_policy", "blueprint"}
                ),
                "identity": {"kind": "Blueprint", "name": "old"},
                "definition": definition,
                "definition_digest": compute_procedure_definition_digest(definition).tagged,
                "pins": [*(p.model_dump() for p in procedure.pins), pin],
            }
        )
        path, content, tag = (
            "blueprints/old.json",
            render_blueprint(blueprint),
            "cruxible-blueprint-v2",
        )
    elif family == "query":
        query = QueryDefinition.model_validate(
            {
                "artifact_format": "playbill-query-definition-v2",
                "identity": {"kind": "QueryDefinition", "name": "old"},
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
        path, content, tag = (
            "query-definitions/old.json",
            render_query_definition(query),
            "cruxible-query-definition-v3",
        )
    elif family == "capture":
        contract = CaptureContract(
            identity={"kind": "CaptureContract", "name": "old"},
            shape="http_response",
            replay="exact",
            components=capture_shape_components("http_response", "exact"),
            selection_budget=CaptureSelectionBudget(max_bytes=100, max_rows=1, max_items=1),
            retention_erasure_policy=CaptureRetentionErasurePolicy(
                body_retention="optional",
                erasure="prohibited",
                selector_privacy="direct_allowed",
            ),
        )
        path, content, tag = (
            "capture-contracts/old.json",
            render_capture_contract(contract),
            "cruxible-capture-contract-v3",
        )
    elif family == "policy":
        policy = SourceAcquisitionPolicyV1.model_validate(
            {
                "identity": {"kind": "SourceAcquisitionPolicy", "name": "old"},
                "inputs": [
                    {
                        "input_name": "observation",
                        "requirement": "required",
                        "permitted_replayability": ["exact"],
                        "max_age": {"microseconds": 30},
                        "on_unavailable": "refuse",
                        "on_stale": "refuse",
                        "on_oversized": "refuse",
                        "on_conflict": "refuse",
                    }
                ],
                "coherence": {"kind": "independent"},
            }
        )
        path, content, tag = (
            "source-acquisition-policies/old.json",
            render_acquisition_policy(policy),
            "playbill-source-acquisition-policy-v2",
        )
    elif family == "provider":
        provider = build()
        interface = accepted_interface()
        from cruxible_client.contracts.provider_interfaces import render_provider_interface

        parent[interface.path] = render_provider_interface(interface.registration)
        path, content, tag = (
            provider_path(provider.identity.name),
            render_provider(provider),
            "cruxible-provider-v5",
        )
    else:
        provider = ProviderV1.model_validate(
            {
                "identity": {"kind": "Provider", "name": "old"},
                "control_domain": "test",
                "capture_contract_digests": [],
                "signing_keys": [
                    {
                        "key_id": "primary",
                        "public_key": "11" * 32,
                        "valid_from": "2026-08-16T12:00:00Z",
                    }
                ],
            }
        )
        path, content, tag = (
            "providers/old.json",
            render_provider(provider),
            "cruxible-provider-identity-v2",
        )
    parent[path] = content
    state = parse_dependency_artifact(path, content)
    if family in {"provider", "signer"}:
        legacy = render_provider(_retired_provider(provider, by_name=False))
        retired = render_provider(_retired_provider(provider, by_name=True))
    else:
        legacy = _retirement(path, content, by_name=False)
        retired = _retirement(path, content, by_name=True)
    import json

    old_wire, wire = json.loads(content), json.loads(retired)
    assert json.loads(legacy)["artifact_format"] == old_wire["artifact_format"]
    assert wire["artifact_format"] == tag
    assert wire["identity"] == old_wire["identity"]
    assert wire["lifecycle"] == {"state": "retired", "predecessor_digest": state.artifact_digest}
    assert pretty_canonical_bytes(old_wire) == content
    assert "pins" not in wire
    if family == "policy":
        assert wire["overrides"]["observation"]["microseconds"] == 30
    result = evaluate_proposal_tree(
        base_tree=parent,
        current_tree=parent,
        proposed_tree={**parent, path: retired},
        current=current,
        bodies=instance.body_store(),
        timestamp="2026-10-25T04:00:00.000000Z",
        rebased=False,
        actor_id="owner",
    )
    if family == "blueprint":
        assert result.candidate is None
        assert any("author a Procedure with slots" in d.message for d in result.diagnostics)
        assert parent[path] == content
    else:
        assert result.diagnostics == ()
        assert result.candidate is not None


def test_named_line_is_unchanged_on_compatible_revision_and_stopped_only_on_retire_with(tmp_path):
    definition = _definition()
    procedure = name_procedure(
        _artifact(
            definition.model_copy(
                update={
                    "nodes": (
                        ConstantNode(
                            node_id="result",
                            contract_out=definition.contract_out,
                            fields={},
                            as_="result",
                        ),
                    )
                }
            )
        )
    )
    line = name_line(_line(requested_rung=1)[0]).model_copy(update={"parameters": {}})
    trigger = name_trigger(
        line_trigger(
            "named-clock", line=line.identity.name, schedule=CadenceSchedule(interval_seconds=60)
        )
    )
    ppath = "procedures/triage.json"
    lpath, tpath = line_spec_path(line.identity.name), trigger_path(trigger.identity.name)
    instance, _ = initialize_local(tmp_path)
    accept_tree(
        instance,
        {
            ppath: render_procedure(procedure),
            lpath: render_line_spec(line),
            tpath: render_trigger(trigger),
        },
        name="named-kit-world",
    )
    coordinate = instance.accepted_coordinate()
    tree = instance.immutable_tree_at(coordinate.git_oid)
    with instance.bind_accepted_projection(coordinate) as projection:
        for retired in (False, True):
            successor = procedure.model_copy(
                update={
                    "lifecycle": ArtifactLifecycle(
                        state="retired" if retired else "live",
                        predecessor_digest=procedure_artifact_digest(procedure).tagged,
                    ),
                }
            )
            writes = {ppath: render_procedure(successor)}
            retire_with = {procedure.identity.qualified} if retired else set()
            carries, stops = _planned_automation(
                projection.typed.connection,
                tree,
                (procedure.identity,),
                writes,
                retire_with,
                by_name=True,
            )
            assert carries == ()
            assert {stop.identity for stop in stops} == (
                {line.identity.qualified, trigger.identity.qualified} if retired else set()
            )
            settled = settle_changes(
                tree,
                writes=writes,
                connection=projection.typed.connection,
                retire_with=frozenset(retire_with),
                by_name=True,
            )
            assert settled.refused == []
            assert {stop.identity for stop in settled.stops} == {stop.identity for stop in stops}
            if not retired:
                assert set(settled.writes) == {ppath}
                assert tree[lpath] == render_line_spec(line)
                assert tree[tpath] == render_trigger(trigger)
