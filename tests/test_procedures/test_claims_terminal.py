"""Claims are templates: only core chooses identity, revisions, evidence and authority."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.authoring.inputs import SubjectInput
from cruxible_client.contracts.captures import capture_contract_path, render_capture_contract
from cruxible_client.contracts.claim_types import (
    ClaimPrecedence,
    ClaimType,
    claim_type_path,
    render_claim_type,
)
from cruxible_client.contracts.claims import parse_claim
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionRule,
)
from cruxible_client.contracts.procedure_mandates import (
    procedure_mandate_path,
    render_procedure_mandate,
)
from cruxible_client.contracts.procedures.claims import ClaimsNode
from cruxible_client.contracts.procedures.line_specs import (
    line_spec_digest,
    line_spec_path,
    parse_line_spec,
)
from cruxible_client.contracts.shapes import Contract, contract_path, render_contract
from cruxible_client.contracts.subject_kinds import render_subject_kind, subject_kind_path
from cruxible_core.service.authoring.documents import service_activate_playbill_proposal
from tests.core_support._identity_support import accept_tree
from tests.core_support._light_capture_support import record_contract
from tests.core_support._subject_kind_support import keyed_subject_kind
from tests.support.claim_types import claim_type as build_claim_type
from tests.support.mandates import procedure_mandate
from tests.support.operation_world import (
    author,
    author_line,
    author_procedure,
    operation_build,
    world,
)
from tests.support.procedures import procedure_limits, procedure_spec

KIND = "security.vulnerability"
PREDICATE = KIND + ".status"
ROW = Contract.model_validate(
    {
        "name": "test.claim-row",
        "version": 1,
        "fields": {"status": "enum[open, closed, unknown]", "note": "string{max_len: 256}?"},
    }
)


def claim_type(
    name: str = PREDICATE, value: str = "enum[open, closed, unknown]", **changes: Any
) -> ClaimType:
    from cruxible_client.contracts.shapes import BoolType, EnumType, parse_type

    typed = parse_type(value)
    schema = {"type": "boolean"} if isinstance(typed, BoolType) else {"type": "string"}
    if isinstance(typed, EnumType):
        schema["enum"] = list(typed.members)
    return build_claim_type(
        **{
            "predicate": name,
            "allowed_subject_kinds": [KIND],
            "value": typed,
            "value_type": schema,
            "permitted_roles": ["derivation", "normative", "observation"],
            "default_role": "observation",
            "revision_evidence": "replace",
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicy(
                rules=(
                    ClaimEvidenceAdmissionRule(
                        rule_id="fixture",
                        claim_roles=("derivation", "normative", "observation"),
                        capture_contracts=("test.orders-v1",),
                        admission="direct",
                        subject_binding="exact_claim_subject",
                    ),
                )
            ),
            **changes,
        }
    )


def claims_world(
    tmp_path: Path,
    document: dict[str, Any],
    *,
    types: tuple[ClaimType, ...] = (),
    rows: Contract = ROW,
) -> tuple[Any, Path]:
    import json

    instance = world(tmp_path, operation_build())
    root = tmp_path / "workspace"
    (root / "data").mkdir(parents=True)
    (root / "data" / "doc.json").write_text(json.dumps(document))
    kind = keyed_subject_kind(KIND, (KIND + ".cve_id",))
    key_type = claim_type(
        KIND + ".cve_id",
        "string",
        evidence_requirement="none",
        evidence_admission_policy=ClaimEvidenceAdmissionPolicy(rules=()),
    )
    capture = record_contract()
    all_types = types or (claim_type(),)
    accept_tree(
        instance,
        {
            capture_contract_path(capture.identity.name): render_capture_contract(capture),
            contract_path(rows.name, rows.version): render_contract(rows),
            subject_kind_path(KIND): render_subject_kind(kind),
            **{claim_type_path(t.predicate): render_claim_type(t) for t in (*all_types, key_type)},
        },
        name="claims-vocabulary",
    )
    for index, key in enumerate(dict.fromkeys(r["id"] for r in document.get("entries", []))):
        author(
            instance,
            SubjectInput(
                kind="subject",
                subject_kind=KIND,
                key={"cve_id": key},
                rationale="Watched vulnerability.",
            ),
            name=f"watched-{index}",
        )
    return instance, root


def claims_spec(
    *,
    mappings: list[dict[str, Any]] | None = None,
    steps: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> Any:
    return procedure_spec(
        "test.claims",
        reference_text=True,
        input={},
        output={},
        slots=extra.pop(
            "slots",
            {
                "fetch": {
                    "in": "workspace.file.request@1",
                    "out": "workspace.file.result@1",
                    "effect": "pure",
                }
            },
        ),
        hard_caps=procedure_limits(provider_calls=1, capture_bytes=65536),
        steps=steps
        or [
            {
                "call": "doc",
                "slot": "fetch",
                "captures": {"capture": "test.orders-v1"},
                "input": {
                    "logical_source": "commerce.production.orders",
                    "relative_path": "data/doc.json",
                    "coordinate_type": "database-snapshot-v1",
                    "coordinate": {"lsn": "workspace"},
                    "selector_type": "relation-primary-key-v1",
                    "selector": {"id": 7, "relation": "orders"},
                },
            },
            {
                "extract": "entries",
                "capture": "$steps.doc.capture",
                "records": "/entries/*",
                "row": ROW.ref.spelling,
                "key": {"cve_id": {"at": "id", "type": f"key_part<{KIND}:cve_id>"}},
                "fields": {"status": {"at": "status"}, "note": {"at": "note"}},
            },
            {
                "id": "write",
                "for_each": "$steps.entries.records",
                "subject": {"kind": KIND, "key": {"cve_id": "$item.key.cve_id"}},
                "claims": mappings or [{"type": PREDICATE, "value": "$item.fields.status"}],
            },
        ],
        **extra,
    )


def install(
    instance: Any,
    spec: Any,
    *,
    line_name: str = "test.claims-line",
    authority: str = "propose",
    parameters: dict[str, Any] | None = None,
    findings: tuple[str, ...] = (),
) -> None:
    author_procedure(instance, spec)
    author_line(
        instance,
        name=line_name,
        procedure=spec.procedure,
        parameters=parameters or {},
        max_authority=authority,
        findings={"inbox": list(findings)},
    )
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    path = line_spec_path(line_name)
    line = parse_line_spec(tree[path], path=path)
    mandate = procedure_mandate(
        line_name,
        namespace=("claims", "subjects"),
        line=ArtifactPin(
            role="line", target=line.identity, artifact_digest=line_spec_digest(line).tagged
        ),
    )
    accept_tree(
        instance,
        {procedure_mandate_path(mandate.identity.name): render_procedure_mandate(mandate)},
        name=line_name + "-mandate",
    )


def run(instance: Any, root: Path, *, minute: int = 0, line: str = "test.claims-line") -> Any:
    from datetime import timedelta

    from cruxible_core.documents.workspace_file import WorkspaceFileReader
    from cruxible_core.runtime.provider_runtime import ProviderRuntimeOperator
    from cruxible_core.service.procedures.procedure_runs import (
        LineRunRequest,
        service_run_playbill_line,
    )
    from tests.support.operation_world import NOW, actor
    from tests.test_procedures.test_completeness_transfer import _Clock

    return service_run_playbill_line(
        instance,
        path_identity_digest=line,
        request=LineRunRequest(line=line),
        actor_context=actor(),
        caller_rung=2,
        daemon_clock=_Clock(NOW + timedelta(minutes=minute)),
        provider_runtime_operator=ProviderRuntimeOperator(root.parent / "state"),
        workspace_file_reader=WorkspaceFileReader(
            instance_id=instance.descriptor.instance_id,
            operating_profile="local",
            attached_roots=(root,),
            managed_roots=(instance.root,),
        ),
    )


def candidate_claims(instance: Any, state: Any) -> list[Any]:
    if state.status != "succeeded":
        raise AssertionError(str(state.terminal.model_dump(mode="json")))
    assert state.terminal_egress, state
    assert state.terminal_egress[0].proposal_id, state.terminal_egress
    evidence = instance.proposal_evidence()
    evaluation = evidence.read_evaluation(state.terminal_egress[0].proposal_id)
    candidate = evidence.read_candidate(evaluation.candidate_digest)
    tree = instance.proposal_tree(
        evaluation.evaluated_tree_oid, proposal_id=state.terminal_egress[0].proposal_id
    )
    return [
        parse_claim(tree[m.path], path=m.path)
        for m in candidate.members
        if m.path.startswith("claims/")
    ]


def activate(instance: Any, state: Any) -> None:
    service_activate_playbill_proposal(
        instance, proposal_id=state.terminal_egress[0].proposal_id, activated_by="owner"
    )


def test_claims_template_proposes_and_rerun_is_unchanged(tmp_path: Path) -> None:
    instance, root = claims_world(
        tmp_path, {"entries": [{"id": "CVE-2026-0001", "status": "open"}]}
    )
    install(instance, claims_spec())
    first = run(instance, root)
    (claim,) = candidate_claims(instance, first)
    assert claim.statement.object.value == "open"
    assert claim.backing.procedure_evidence.law == "claim-evidence-v2"
    assert claim.backing.procedure_evidence.value.basis == "extracted"
    activate(instance, first)
    second = run(instance, root, minute=1)
    assert second.status == "succeeded", second
    assert second.terminal_egress[0].proposal_id is None
    assert all(child.unchanged for child in second.terminal_egress[0].children)


@pytest.mark.parametrize("feature", ["when_absent", "covers", "enumeration", "mirror"])
def test_absence_instructions_are_refused_until_b4b(feature: str) -> None:
    from cruxible_client.contracts.artifacts import ArtifactNameRef

    with pytest.raises(ValidationError, match="lands in B4b"):
        ClaimsNode(
            node_id="write",
            for_each="$steps.entries.records",
            subject={"kind": KIND, "key": {"cve_id": "$item.key.cve_id"}},
            claims=(
                {
                    "type": ArtifactNameRef(
                        target=ArtifactIdentity(kind="ClaimType", name=PREDICATE)
                    ),
                    "value": "open",
                },
            ),
            **{feature: "mirror"},
        )


def test_replaces_is_one_role_policy() -> None:
    policy = ClaimPrecedence()
    assert "normative" not in policy.replacements("observation")
    assert policy.replacements("observation", ("observation",)) == ("observation",)
    with pytest.raises(ValueError, match="claims_replaces_forbidden"):
        policy.replacements("observation", ("normative",))


def test_invalid_mapping_is_excluded_and_valid_sibling_writes(tmp_path: Path) -> None:
    note = claim_type(KIND + ".note", "string")
    instance, root = claims_world(
        tmp_path,
        {"entries": [{"id": "CVE-2026-0001", "status": "Maybe", "note": "keep"}]},
        types=(claim_type(), note),
    )
    install(
        instance,
        claims_spec(
            mappings=[
                {"type": PREDICATE, "value": "$item.fields.status"},
                {"type": note.predicate, "value": "$item.fields.note"},
            ]
        ),
        findings=("extract_refused",),
    )
    state = run(instance, root)
    (claim,) = candidate_claims(instance, state)
    assert claim.statement.predicate == note.predicate
    (diagnostic,) = state.terminal_egress[0].claims_diagnostics
    assert diagnostic.kind == "extract_refused" and diagnostic.lexeme == "Maybe"
    assert diagnostic.problem == "not_in_enum"
    # The diagnostic is a typed inbox item, not a proposed invalid Claim.
    from cruxible_core.exhaust.line_inbox import LineInboxStore

    inbox = LineInboxStore(instance)
    with inbox.locked() as connection:
        assert connection.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1


def test_one_valued_change_revises_the_eligible_live_claim(tmp_path: Path) -> None:
    import json

    instance, root = claims_world(
        tmp_path, {"entries": [{"id": "CVE-2026-0001", "status": "open"}]}
    )
    install(instance, claims_spec())
    first = run(instance, root)
    (old,) = candidate_claims(instance, first)
    activate(instance, first)
    (root / "data/doc.json").write_text(
        json.dumps({"entries": [{"id": "CVE-2026-0001", "status": "closed"}]})
    )
    second = run(instance, root, minute=1)
    (revised,) = candidate_claims(instance, second)
    assert revised.identity == old.identity
    assert revised.statement.object.value == "closed"
    assert revised.lifecycle.predecessor_digest is not None


def test_a_normative_holder_is_protected_before_proposal(tmp_path: Path) -> None:
    instance, root = claims_world(
        tmp_path, {"entries": [{"id": "CVE-2026-0001", "status": "open"}]}
    )
    install(
        instance,
        claims_spec(
            mappings=[{"type": PREDICATE, "value": "$item.fields.status", "role": "normative"}]
        ),
    )
    first = run(instance, root)
    (held,) = candidate_claims(instance, first)
    activate(instance, first)
    # A second Procedure writes a different observation in the same slot.
    import json

    (root / "data/doc.json").write_text(
        json.dumps({"entries": [{"id": "CVE-2026-0001", "status": "closed"}]})
    )
    from tests.support.procedures import procedure_spec as spec_builder

    observed = claims_spec()
    observed = spec_builder(
        base=observed.model_dump(mode="python", by_alias=True), procedure="test.observed"
    )
    install(instance, observed, line_name="test.observed-line")
    state = run(instance, root, line="test.observed-line", minute=1)
    assert state.status == "succeeded", state.terminal
    egress = state.terminal_egress[0]
    assert egress.proposal_id is None
    (diagnostic,) = egress.claims_diagnostics
    assert diagnostic.kind == "held_disagrees"
    assert diagnostic.claim == held.identity.name and diagnostic.role == "normative"
