"""Exact reference reads retain the full-tree lowerer's bytes and refusals."""

from dataclasses import asdict

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.captures import capture_contract_path, render_capture_contract
from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type
from cruxible_client.contracts.procedures.artifacts import parse_procedure
from cruxible_client.contracts.query.definitions import (
    query_definition_digest,
    query_definition_path,
    render_query_definition,
)
from cruxible_core.authoring import lowering
from cruxible_core.derived.derived_state import SnapshotTree
from tests.core_support._pc_c_support import capture_contract
from tests.test_authoring.test_authoring_history_reuse import _intent
from tests.test_authoring.test_authoring_procedures import (
    _change_set_query,
    _list_contract_payload,
    _triage_definition,
    _world_holding_a_document,
)
from tests.test_claims.test_claims import _claim_type


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    coordinator, _actor = _world_holding_a_document(
        tmp_path_factory.mktemp("reference-lowering"), historical=True
    )
    instance = coordinator.instance
    base = instance.accepted_coordinate()
    return instance, base, instance.immutable_tree_at(base.git_oid)


def _reference(identity, *, candidate=False, role="query"):
    return {
        "tag": (
            "playbill-authoring-candidate-reference-v1"
            if candidate
            else "playbill-authoring-artifact-reference-v1"
        ),
        "role": role,
        "target": identity.model_dump(mode="json"),
        "resolution": "candidate_in_change_set" if candidate else "accepted_at_intent_base",
    }


def _outcome(instance, base, tree, payload, **kwargs):
    try:
        result = lowering._lower_procedure(
            instance,
            intent=_intent().model_copy(update={"payload": payload}),
            base=base,
            base_tree=tree,
            **kwargs,
        )
    except lowering.AuthoringLoweringError as exc:
        return asdict(exc)
    return result.changed_members, result.resolved_authoring


@pytest.mark.parametrize("case", ["live", "missing", "retired", "wrong-kind", "malformed"])
def test_reference_bytes_pins_and_typed_refusals_match_full_tree(world, monkeypatch, case):
    instance, base, original = world
    query = _change_set_query(named=False)
    if case == "retired":
        query = query.model_copy(update={"lifecycle": ArtifactLifecycle(state="retired")})
    target = query.identity
    if case == "wrong-kind":
        target = ArtifactIdentity(kind="Provider", name=query.identity.name)
    blobs = dict(original)
    path = query_definition_path(query.identity.name)
    if case != "missing":
        blobs[path] = b"{}" if case == "malformed" else render_query_definition(query)
    payload = _list_contract_payload().model_copy(
        update={"definition": _triage_definition(query=_reference(target))}
    )
    tree = SnapshotTree(blobs)
    actual = _outcome(instance, base, tree, payload)
    # Identity selection is the only change: bypass it to exercise the old
    # full-tree parse, resolution, graph validation and artifact rendering.
    monkeypatch.setattr(lowering, "_reference_blobs", lambda tree, *_args, **_kwargs: tree)
    assert actual == _outcome(instance, base, tree, payload)
    if case in {"missing", "wrong-kind", "malformed"}:
        assert actual["code"] == (
            "cruxible.authoring.lowering_invalid"
            if case == "malformed"
            else "cruxible.authoring.artifact_reference_unresolved"
        )
    else:
        procedure = parse_procedure(actual[0][0][1], path="procedures/triage.json")
        assert next(pin for pin in procedure.pins if pin.role == "query").artifact_digest == (
            query_definition_digest(query).tagged
        )


def test_staged_claim_type_and_capture_contract_match_full_tree(world, monkeypatch):
    # B2: a source/call node names a slot (a plain string, validated against the
    # Procedure's own `slots` declaration, not a candidate artifact reference) —
    # there is no longer a provider/interface candidate reference to resolve at
    # authoring time; binding to an exact provider build + operation happens at
    # Line acceptance (C2), not here. This test keeps the surviving part of its
    # original point: staged claim-type and capture-contract candidate
    # references still resolve and pin exactly like the full-tree lowerer.
    instance, base, original = world
    claim_type, contract = _claim_type(), capture_contract()
    accepted = SnapshotTree(dict(original))
    staged = accepted.fork()
    staged.update(
        {
            claim_type_path(claim_type.predicate): render_claim_type(claim_type),
            capture_contract_path(contract.identity.name): render_capture_contract(contract),
        }
    )
    definition = _triage_definition()
    definition["budget"]["max_provider_calls"] = 1
    definition["hard_caps"]["max_provider_calls"] = 1
    definition["slots"] = {
        **definition.get("slots", {}),
        "fetch": {
            "in": "workspace.file.request@1",
            "out": "workspace.file.result@1",
            "effect": "external_read",
        },
    }
    definition["nodes"] = [
        {
            "kind": "source",
            "node_id": "fetch",
            "capture_contract": _reference(
                contract.identity, candidate=True, role="capture-contract"
            ),
            "slot": "fetch",
            "request": {},
            "as": "captured",
        },
        {
            "kind": "state_claim",
            "node_id": "read",
            "claim_type": _reference(claim_type.identity, candidate=True, role="claim-type"),
            "subject_kind": "project.work_item",
            "subject_id": "wi-42",
            "as": "status",
        },
        *definition["nodes"],
    ]
    payload = _list_contract_payload().model_copy(update={"definition": definition})
    candidates = frozenset(item.identity.qualified for item in (claim_type, contract))
    kwargs = {"accepted_reference_tree": accepted, "candidate_identities": candidates}
    actual = _outcome(instance, base, staged, payload, **kwargs)
    assert isinstance(actual, tuple), actual
    monkeypatch.setattr(lowering, "_reference_blobs", lambda tree, *_args, **_kwargs: tree)
    assert actual == _outcome(instance, base, staged, payload, **kwargs)
    procedure = parse_procedure(actual[0][0][1], path="procedures/triage.json")
    assert candidates <= {pin.target.qualified for pin in procedure.pins}


def test_duplicate_envelopes_remain_unresolved(world, monkeypatch):
    from dataclasses import replace

    instance, base, original = world
    query = _change_set_query(named=False)
    tree = SnapshotTree(
        {**original, query_definition_path(query.identity.name): render_query_definition(query)}
    )
    parse = lowering._parse_reference_tree

    def duplicate(*args, **kwargs):
        parsed = parse(*args, **kwargs)
        envelope = next(
            item for item in parsed.envelopes if item.identity == query.identity.qualified
        )
        return replace(parsed, envelopes=(*parsed.envelopes, envelope))

    monkeypatch.setattr(lowering, "_parse_reference_tree", duplicate)
    payload = _list_contract_payload().model_copy(
        update={"definition": _triage_definition(query=_reference(query.identity))}
    )
    result = _outcome(instance, base, tree, payload)
    assert result["code"] == "cruxible.authoring.artifact_reference_unresolved"


@pytest.mark.parametrize(
    ("identity", "path"),
    [
        ("ApprovalPolicy:instance", "governance/approval-policy.json"),
        ("ProcedureRuntimePolicy:instance", "governance/procedure-runtime-policy.json"),
        ("document:design", "documents/design.json"),
        ("Subject:project.work_item/wi-42", "subjects/project.work_item/wi-42.json"),
        ("ClaimType:project.work_item.status", "claim-types/project.work_item/status.json"),
        ("Claim:CLM-" + "a" * 32, "claims/aa/CLM-" + "a" * 32 + ".json"),
        ("ClaimAttestation:sha256:" + "b" * 64, "attestations/bb/" + "b" * 64 + ".json"),
        ("Provider:demo.provider", "providers/demo.provider.json"),
        ("Provider:demo.provider/" + "c" * 64, "providers/demo.provider/" + "c" * 64 + ".json"),
        # ProviderInterface, Blueprint and SourceAcquisitionPolicy are removed
        # (B2, C12): no envelope kind projects them, so every identity of
        # these kinds is an unresolved reference regardless of shape.
        ("ProviderInterface:demo.interface", None),
        ("ProviderInterface:demo.interface/" + "d" * 64, None),
        ("Blueprint:demo", None),
        ("Procedure:demo", "procedures/demo.json"),
        ("Line:demo", "lines/demo.json"),
        ("Trigger:demo", "triggers/demo.json"),
        ("SubjectKind:project.work_item", "subject-kinds/project.work_item.json"),
        ("QueryDefinition:demo", "query-definitions/demo.json"),
        ("CaptureContract:demo", "capture-contracts/demo.json"),
        ("ResolutionContract:demo", "resolution-contracts/demo.json"),
        ("SourceAcquisitionPolicy:demo", None),
        ("ProcedureMandate:demo", "procedure-mandates/demo.json"),
        ("ExhaustPromotion:demo", "exhaust-promotions/demo.json"),
        ("Contract:carried", None),
        ("Document:design", None),
        ("ApprovalPolicy:other", None),
        ("ProcedureRuntimePolicy:other", None),
        ("Subject:missing-kind", None),
        ("ClaimType:malformed/name", None),
    ],
)
def test_reference_paths_cover_every_envelope_kind(identity, path):
    from cruxible_client.contracts.artifacts import parse_artifact_identity

    assert lowering._reference_path(parse_artifact_identity(identity)) == path
