"""Claims bind declared keys at the accepted coordinate and pin existing Subjects.

Identity changes in a record cannot redirect sibling mappings: existing keys
resolve to exact Subject addresses, missing create:never keys are filtered,
and create:if_absent assigns a new Subject only when the accepted key is absent.
Aliases are values, not an alternate interpretation of a declared key.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.claim_types import ClaimType, claim_type_path, render_claim_type
from cruxible_client.contracts.claims import parse_claim
from cruxible_client.contracts.procedure_mandates import (
    procedure_mandate_path,
    render_procedure_mandate,
)
from cruxible_client.contracts.subject_kinds import render_subject_kind, subject_kind_path
from cruxible_client.contracts.subjects import parse_subject
from cruxible_core.service.authoring.documents import service_inspect_playbill_proposal
from tests.core_support._knowledge_loop_support import accept_proposal
from tests.core_support._light_capture_support import file_contract as capture_contract
from tests.core_support._subject_kind_support import bare_slug_claim_type, keyed_subject_kind
from tests.test_procedures import test_procedure_proposal_delivery as delivery
from tests.test_procedures import test_procedure_source_runs as fixtures

KIND = "security.package"
SLUG = f"{KIND}.slug"
SEVERITY = f"{KIND}.severity"


def _self_source(text: str) -> dict[str, str]:
    return {
        "tag": "playbill-self-source-body-v1",
        "content_base64": base64.b64encode(text.encode("utf-8")).decode("ascii"),
    }


def subject_item(slug: str = "Click", **overrides: Any) -> dict[str, object]:
    item: dict[str, object] = {
        "tag": "playbill-procedure-subject-proposal-item-v1",
        "subject_kind": KIND,
        "key": {"slug": slug},
        "rationale": "The advisory names this package.",
        "source": _self_source(f"package: {slug}\n"),
    }
    item.update(overrides)
    return item


def keyed_claim_item(slug: str = "click") -> dict[str, object]:
    return {
        "tag": "playbill-procedure-claim-proposal-item-v3",
        "statement": {
            "tag": "playbill-procedure-claim-statement-v1",
            "subject": {"subject_kind": KIND, "key": {"slug": slug}},
            "predicate": SEVERITY,
            "qualifier": None,
            "object": {"kind": "literal", "value": "$steps.result.severity"},
            "role": "observation",
            "effective_from": None,
            "effective_until": None,
        },
        "rationale": "Severity as read from the accepted advisory document.",
        "revises": None,
    }


def _severity_type() -> ClaimType:
    template = delivery._claim_type(capture_contract().identity.name)
    return template.model_copy(
        update={
            "identity": ArtifactIdentity(kind="ClaimType", name=SEVERITY),
            "predicate": SEVERITY,
            "allowed_subject_kinds": (KIND,),
        }
    )


SELF_SOURCE_NAMESPACE = "capture-contracts/playbill.coordinator-self-source-v1.json"


def subject_world(  # type: ignore[no-untyped-def]
    tmp_path: Path,
    templates: tuple[object, ...],
    *,
    namespace: tuple[str, ...] = (SELF_SOURCE_NAMESPACE, "claims", f"subjects/{KIND}"),
    document: bool = False,
):
    """The proposal world, with packages keyed by slug and a mandate reaching them."""

    slug = bare_slug_claim_type(KIND)
    severity = _severity_type()
    instance, owner, root, line, procedure = delivery.proposal_world(
        tmp_path,
        templates=templates,
        mandate=None,
        document=document,
        claim_type=severity,
        additional_members={
            claim_type_path(SLUG): render_claim_type(slug),
            subject_kind_path(KIND): render_subject_kind(keyed_subject_kind(KIND, (SLUG,))),
        },
    )
    # Key Claims citing the item's own words install the coordinator self-source
    # CaptureContract with the first such Claim, so the default mandate reaches it.
    mandate = fixtures._line_mandate(line, procedure).model_copy(update={"namespace": namespace})
    fixtures._accept_more(
        instance,
        owner,
        {
            claim_type_path(SLUG): render_claim_type(slug),  # type: ignore[arg-type]
            claim_type_path(SEVERITY): render_claim_type(severity),
            subject_kind_path(KIND): render_subject_kind(keyed_subject_kind(KIND, (SLUG,))),
            procedure_mandate_path(mandate.identity.name): render_procedure_mandate(mandate),
        },
        name="package-kind",
    )
    return instance, owner, root, line


def _delivered(instance, state):  # type: ignore[no-untyped-def]
    assert state.status == "succeeded", (state.terminal, state.terminal_egress)
    (egress,) = state.terminal_egress
    assert egress.verdict == "delivered", egress
    return egress


@pytest.mark.parametrize("bare", [False, True])
def test_a_subject_item_creates_by_key_and_a_claim_item_names_it_by_key(
    tmp_path: Path, bare: bool
) -> None:
    template = keyed_claim_item()
    if bare:
        template["statement"]["subject"]["key"] = "click"  # type: ignore[index]
    instance, owner, root, line = subject_world(tmp_path, (subject_item(), template))

    egress = _delivered(instance, delivery.run_line(instance, root, line))

    created, claimed = egress.children
    # Core named the package from its canonical slug, never from the run.
    assert created.path == f"subjects/{KIND}/click.json"
    assert created.path in egress.target_paths and not created.unchanged
    assert claimed.path is not None and claimed.path in egress.target_paths
    assert any(path.startswith("claims/") for path in egress.target_paths)

    from tests.core_support._retirement_support import activate_proposal

    # Accepted through the service, so the record binds this exact admission.
    activate_proposal(instance, owner, egress.proposal_id)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    assert parse_subject(tree[created.path], path=created.path).subject_id == "click"
    claim = parse_claim(tree[claimed.path], path=claimed.path)
    assert claim.statement.subject.artifact_path == created.path
    keys = [
        parse_claim(tree[path], path=path)
        for path in tree
        if path.startswith("claims/")
        and parse_claim(tree[path], path=path).statement.predicate == SLUG
    ]
    assert [item.statement.object.model_dump(mode="json")["value"] for item in keys] == ["click"]
    # Each item's reason is kept with the member it explains, the Subject included.
    from cruxible_client.contracts.get_reads import GetRequest
    from cruxible_core.service.discovery.get import service_playbill_get

    why = service_playbill_get(
        instance,
        request=GetRequest(ref=f"Subject:{KIND}/click", detail="why"),
        access=delivery._READER,
    )
    assert why.reason is not None
    assert why.reason.reason == "Severity as read from the accepted advisory document."


def test_a_key_a_live_subject_holds_reuses_it_and_creates_nothing(tmp_path: Path) -> None:
    instance, owner, root, line = subject_world(tmp_path, (subject_item(),))
    first = _delivered(instance, delivery.run_line(instance, root, line))
    accept_proposal(
        instance, owner, service_inspect_playbill_proposal(instance, proposal_id=first.proposal_id)
    )
    base = instance.accepted_coordinate()

    # The same key now names the live package: the item creates nothing.
    state = delivery.run_line(
        instance, root, line, at=fixtures.NOW.replace(minute=fixtures.NOW.minute + 1)
    )

    assert state.status == "succeeded", state.terminal
    (egress,) = state.terminal_egress
    assert all(child.unchanged for child in egress.children)
    assert f"subjects/{KIND}/click.json" not in egress.target_paths
    assert f"subjects/{KIND}/click.json" in instance.tree_at(base.git_oid)
    assert egress.proposal_id is None
    assert instance.accepted_coordinate() == base


def test_a_key_reference_no_subject_holds_refuses_naming_the_creation(tmp_path: Path) -> None:
    # Native create:never filters missing Subjects without proposing a creation.
    instance, _owner, root, line = subject_world(tmp_path, (keyed_claim_item("requests"),))
    base = instance.accepted_coordinate()
    egress = _delivered(instance, delivery.run_line(instance, root, line))
    assert egress.proposal_id is None and egress.target_paths == () and egress.children == ()
    assert instance.accepted_coordinate() == base


def test_a_subject_item_of_an_undeclared_kind_refuses(tmp_path: Path) -> None:
    from cruxible_client.contracts.diagnostics import CompilerDiagnostic
    from cruxible_core.procedures.claims_typing import (
        ClaimsTypingRefused,
        validate_claims_templates,
    )
    from tests.core_support._candidate_support import submit_member_candidate
    from tests.support.procedures import procedure_members

    instance, _owner, _root = _identity_world(tmp_path, "click")
    procedure, contracts = _identity_procedure(instance, kind="security.vendor")
    base = instance.accepted_coordinate()
    with instance.bind_accepted_projection(base) as projection:
        with pytest.raises(ClaimsTypingRefused, match="SubjectKind:security.vendor must be live"):
            validate_claims_templates(
                procedure.definition, lambda identity: projection.typed.source(identity.qualified)
            )
    inspection = submit_member_candidate(
        instance,
        members={**procedure_members(procedure), **contracts},
        actor_id="owner",
        proposal_name="undeclared-kind",
        proposal_family="procedure",
        timestamp=fixtures.SECOND_STAMP,
    )
    assert inspection.proposal.candidate is None
    (refused,) = inspection.proposal.evaluation.diagnostics
    assert isinstance(refused, CompilerDiagnostic)
    assert refused.code == ClaimsTypingRefused.code
    assert "SubjectKind:security.vendor" in refused.message
    assert instance.accepted_coordinate() == base


def test_create_new_on_a_unique_kind_refuses(tmp_path: Path) -> None:
    from cruxible_client.contracts.procedures.claims import ClaimsSubject

    with pytest.raises(
        ValidationError, match=r"create\n\s+Input should be 'never' or 'if_absent'"
    ) as refused:
        ClaimsSubject(kind=KIND, key={"slug": "click"}, create="create_new")
    assert refused.value.errors()[0]["type"] == "literal_error"
    assert refused.value.errors()[0]["loc"] == ("create",)


# -- Identity changes cannot redirect claims bound at the accepted coordinate -----------


def _identity_world(tmp_path: Path, *slugs: str):
    """Accepted packages and direct evidence rules for identity-changing templates."""
    from cruxible_client.contracts.artifacts import ArtifactLifecycle
    from cruxible_client.contracts.authoring.inputs import SubjectInput
    from cruxible_client.contracts.claim_types import claim_type_digest, parse_claim_type
    from cruxible_client.contracts.policies import (
        ClaimEvidenceAdmissionPolicy,
        ClaimEvidenceAdmissionRule,
    )
    from tests.support.operation_world import author

    instance, owner, root, _line = subject_world(tmp_path, (subject_item(),))
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    updates = {}
    for predicate in (SLUG, "semantic.alias"):
        path = claim_type_path(predicate)
        original = parse_claim_type(tree[path], path=path)
        revised = original.model_copy(
            update={
                "allowed_subject_kinds": tuple(
                    sorted(set(original.allowed_subject_kinds) | {KIND})
                ),
                # These probes deliberately move normative identity Claims.
                "precedence": original.precedence.model_copy(
                    update={
                        "revisable_by_procedures": original.precedence.roles,
                    }
                ),
                "evidence_admission_policy": ClaimEvidenceAdmissionPolicy(
                    rules=(
                        ClaimEvidenceAdmissionRule(
                            rule_id="identity-observation",
                            claim_roles=("normative",),
                            capture_contracts=(capture_contract().identity.name,),
                            admission="direct",
                            subject_binding="exact_claim_subject",
                        ),
                    )
                ),
                "lifecycle": ArtifactLifecycle(
                    predecessor_digest=claim_type_digest(original).tagged
                ),
            }
        )
        updates[path] = render_claim_type(revised)
    fixtures._accept_more(instance, owner, updates, name="identity-evidence")
    for index, slug in enumerate(slugs):
        author(
            instance,
            SubjectInput(
                kind="subject",
                subject_kind=KIND,
                key={"slug": slug},
                rationale="The source names this package.",
            ),
            name=f"package-{index}",
        )
    return instance, owner, root


def _identity_procedure(
    instance, *, mappings=None, create="never", kind=KIND, name="identity-probe", expect=()
):
    """One typed claims node over independently keyed source records.

    ``expect`` names the Subjects an absence-retiring terminal expects (its
    mappings then carry their own ``when_absent``); the records carry a count.
    """
    from cruxible_client.contracts.procedures.artifacts import parse_procedure, procedure_path
    from cruxible_client.contracts.procedures.claims import ClaimsNode
    from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
    from cruxible_client.contracts.shapes import (
        Contract,
        contract_digest,
        contract_path,
        render_contract,
    )
    from tests.support.claims_terminals import delivery_definition
    from tests.support.layouts import grown_kind_members, record_set

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    path = procedure_path(fixtures.PROCEDURE_NAME)
    original = parse_procedure(tree[path], path=path)
    definition, contracts = delivery_definition(
        original,
        subject_kind=kind,
        subject_id="click",
        predicates=(SEVERITY,),
        records="/proposals/*",
        create=create == "if_absent",
        within=expect,
        absence="mirror" if expect else None,
    )
    row = Contract.model_validate(
        {
            "name": "security.identity-row",
            "version": 1,
            "fields": {
                "severity": "string{max_len: 256}",
                "new_key": "string{min_len: 1, max_len: 256}?",
                "alias": "string{min_len: 1, max_len: 256}?",
            },
        }
    )
    source, extract, terminal = definition.nodes
    # The identity rows are another record set of the world kind's layout.
    identity_set = "identity_rows_counted" if expect else "identity_rows_all"
    grown = grown_kind_members(
        instance,
        source.captures["capture"].target.name,
        {
            identity_set: record_set(
                at="/proposals/*",
                row=row.ref.model_copy(update={"digest": contract_digest(row).tagged}),
                fields={name: {"at": name} for name in row.fields},
                key={"slug": {"at": "id"}},
                count_at="/count" if expect else None,
            )
        },
    )
    extract = extract.model_copy(
        update={
            "records": identity_set,
            "row": row.ref.model_copy(update={"digest": contract_digest(row).tagged}),
        }
    )
    terminal = ClaimsNode.model_validate(
        {
            **terminal.model_dump(mode="python", by_alias=True),
            "claims": mappings
            or [
                {
                    "type": {"target": ArtifactIdentity(kind="ClaimType", name=SEVERITY)},
                    "value": "$item.fields.severity",
                    "role": "observation",
                }
            ],
        }
    )
    definition = definition.model_copy(update={"name": name, "nodes": (source, extract, terminal)})
    procedure = original.model_copy(
        update={
            "identity": ArtifactIdentity(kind="Procedure", name=name),
            "definition": definition,
            "definition_digest": compute_procedure_definition_digest(definition).tagged,
        }
    )
    return procedure, {
        **{contract_path(c.name, c.version): render_contract(c) for c in (row, contracts[1])},
        **grown,
    }


def _identity_terminal(
    instance,
    owner,
    root,
    records,
    *,
    mappings=None,
    create="never",
    name="identity-probe",
    expect=(),
):
    from cruxible_client.contracts.procedures.line_specs import line_spec_path, render_line_spec
    from tests.support.procedures import procedure_members

    procedure, contracts = _identity_procedure(
        instance, mappings=mappings, create=create, name=name, expect=expect
    )
    line = fixtures._served_line(instance, procedure, fixtures._policy()).model_copy(
        update={
            "identity": ArtifactIdentity(kind="Line", name=name),
            "max_authority": "propose",
        }
    )
    mandate = fixtures._line_mandate(line, procedure).model_copy(
        update={
            "identity": ArtifactIdentity(kind="ProcedureMandate", name=name + "-mandate"),
            "namespace": ("claims", "subjects"),
        }
    )
    fixtures._accept_more(
        instance,
        owner,
        {
            **procedure_members(procedure),
            **contracts,
            line_spec_path(name): render_line_spec(line),
            procedure_mandate_path(mandate.identity.name): render_procedure_mandate(mandate),
        },
        name=name,
    )
    document: dict[str, Any] = {"proposals": records}
    if expect:
        document["count"] = len(records)
    (root / fixtures.RELATIVE_PATH).write_bytes(fixtures.canonical_bytes(document))
    return line


def _mapping(predicate, value, **extra):
    return {
        "type": {"target": ArtifactIdentity(kind="ClaimType", name=predicate)},
        "value": value,
        **extra,
    }


def _identity_claims(instance, egress):
    """Only changed live Claims in this proposal, with exact Subject addresses."""
    inspection = service_inspect_playbill_proposal(instance, proposal_id=egress.proposal_id)
    tree = instance.proposal_tree(inspection.proposal.evaluation.evaluated_tree_oid)
    return [
        parse_claim(tree[path], path=path)
        for path in egress.target_paths
        if path.startswith("claims/")
    ]


def _identity_accept(instance, owner, egress):
    accept_proposal(
        instance, owner, service_inspect_playbill_proposal(instance, proposal_id=egress.proposal_id)
    )


def _holder(instance, key):
    from cruxible_core.procedures.claims_context import AcceptedClaimsContext

    subject = AcceptedClaimsContext(instance, instance.accepted_coordinate()).subject(
        KIND, {"slug": key}
    )
    return None if subject is None else subject.artifact_path


_KEY_MOVE = _mapping(SLUG, "$item.fields.new_key", role="normative")
_SEVERITY = _mapping(SEVERITY, "$item.fields.severity", role="observation")


def test_a_key_move_and_recreation_keep_the_original_claims_subject(tmp_path: Path) -> None:
    instance, owner, root = _identity_world(tmp_path, "click")
    old = f"subjects/{KIND}/click.json"
    line = _identity_terminal(
        instance,
        owner,
        root,
        [{"id": "click", "new_key": "renamed", "severity": "high"}],
        mappings=[_KEY_MOVE, _SEVERITY],
        create="if_absent",
    )
    moved = _delivered(instance, delivery.run_line(instance, root, line))
    assert not any(path.startswith("subjects/") for path in moved.target_paths)
    claims = _identity_claims(instance, moved)
    assert {c.statement.subject.artifact_path for c in claims} == {old}
    severity = next(c for c in claims if c.statement.predicate == SEVERITY)
    _identity_accept(instance, owner, moved)
    assert _holder(instance, "renamed") == old and _holder(instance, "click") is None

    recreation = _identity_terminal(
        instance,
        owner,
        root,
        [{"id": "click", "severity": "low"}],
        create="if_absent",
        name="recreate-key",
    )
    # The old Subject still owns the exact id "click". A newly assigned
    # click-<hex> must not make the terminal's creation reference resolve there.
    base = instance.accepted_coordinate()
    refs = set(delivery._proposal_refs(instance))
    refusal = delivery._refusal(delivery.run_line(instance, root, recreation))
    assert refusal.code == "proposal_lowering_refused"
    assert refusal.details["code"] == "cruxible.procedure.identity_reference_unstable"
    (changed,) = refusal.details["references"]
    assert changed["reference"] == {"subject_kind": KIND, "key": {"slug": "click"}}
    assert changed["frozen"].startswith(f"subjects/{KIND}/click-")
    assert changed["final"] == old
    assert instance.accepted_coordinate() == base
    assert set(delivery._proposal_refs(instance)) == refs
    from datetime import timedelta

    from cruxible_core.service.proposals.proposal_egress import service_recover_proposal_egress

    assert (
        service_recover_proposal_egress(instance, recorded_at=fixtures.NOW + timedelta(minutes=1))
        == {}
    )
    assert _holder(instance, "click") is None and _holder(instance, "renamed") == old
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    from cruxible_client.contracts.claims import claim_path

    retained = parse_claim(
        tree[claim_path(severity.identity.name)], path=claim_path(severity.identity.name)
    )
    assert retained.statement.subject.artifact_path == old and retained.lifecycle.state == "live"


def test_a_missing_key_supplied_by_a_sibling_waits_for_the_next_run(tmp_path: Path) -> None:
    instance, owner, root = _identity_world(tmp_path, "click", "flask")
    flask = f"subjects/{KIND}/flask.json"
    line = _identity_terminal(
        instance,
        owner,
        root,
        [
            {"id": "flask", "new_key": "requests", "severity": "high"},
            {"id": "requests", "severity": "low"},
        ],
        mappings=[_KEY_MOVE, _SEVERITY],
    )
    assert _holder(instance, "requests") is None
    first = _delivered(instance, delivery.run_line(instance, root, line))
    claims = _identity_claims(instance, first)
    assert {(c.statement.predicate, c.statement.object.value) for c in claims} == {
        (SLUG, "requests"),
        (SEVERITY, "high"),
    }
    assert {c.statement.subject.artifact_path for c in claims} == {flask}
    _identity_accept(instance, owner, first)
    assert _holder(instance, "requests") == flask
    (root / fixtures.RELATIVE_PATH).write_bytes(
        fixtures.canonical_bytes({"proposals": [{"id": "requests", "severity": "low"}]})
    )
    from datetime import timedelta

    second = _delivered(
        instance, delivery.run_line(instance, root, line, at=fixtures.NOW + timedelta(minutes=1))
    )
    (changed,) = _identity_claims(instance, second)
    assert changed.statement.subject.artifact_path == flask
    assert changed.statement.predicate == SEVERITY and changed.statement.object.value == "low"


def test_a_keyed_alias_owner_cannot_redirect_a_sibling_key(tmp_path: Path) -> None:
    instance, owner, root = _identity_world(tmp_path, "click", "other name")
    line = _identity_terminal(
        instance,
        owner,
        root,
        [
            {"id": "click", "alias": "other name", "severity": "high"},
            {"id": "other name", "severity": "low"},
        ],
        mappings=[_mapping("semantic.alias", "$item.fields.alias", role="normative"), _SEVERITY],
    )
    egress = _delivered(instance, delivery.run_line(instance, root, line))
    claims = _identity_claims(instance, egress)
    (alias,) = [c for c in claims if c.statement.predicate == "semantic.alias"]
    assert alias.statement.subject.artifact_path == f"subjects/{KIND}/click.json"
    assert alias.statement.qualifier is None and alias.statement.object.value == "other name"
    assert {
        c.statement.subject.artifact_path: c.statement.object.value
        for c in claims
        if c.statement.predicate == SEVERITY
    } == {f"subjects/{KIND}/click.json": "high", f"subjects/{KIND}/other-name.json": "low"}
    _identity_accept(instance, owner, egress)
    assert _holder(instance, "other name") == f"subjects/{KIND}/other-name.json"


def test_a_keyed_owner_moving_its_key_keeps_both_mappings_on_that_owner(tmp_path: Path) -> None:
    instance, owner, root = _identity_world(tmp_path, "click")
    line = _identity_terminal(
        instance,
        owner,
        root,
        [
            {"id": "click", "new_key": "requests", "severity": "high"},
            {"id": "requests", "severity": "low"},
        ],
        mappings=[_KEY_MOVE, _SEVERITY],
    )
    base = instance.accepted_coordinate()
    egress = _delivered(instance, delivery.run_line(instance, root, line))
    assert instance.accepted_coordinate() == base
    claims = _identity_claims(instance, egress)
    assert len(claims) == 2
    assert {c.statement.subject.artifact_path for c in claims} == {f"subjects/{KIND}/click.json"}
    assert {c.statement.object.value for c in claims} == {"requests", "high"}
    _identity_accept(instance, owner, egress)
    assert _holder(instance, "requests") == f"subjects/{KIND}/click.json"
    assert _holder(instance, "click") is None


def test_a_stable_terminal_still_creates_and_names_its_subject(tmp_path: Path) -> None:
    from cruxible_client.contracts.canonical import canonical_bytes

    # A native record per Subject creates and names two stable keys in one terminal.
    instance, owner, root, line = subject_world(tmp_path, (subject_item(), keyed_claim_item()))
    first = _delivered(instance, delivery.run_line(instance, root, line))
    accept_proposal(
        instance, owner, service_inspect_playbill_proposal(instance, proposal_id=first.proposal_id)
    )
    from cruxible_client.contracts.procedures.artifacts import (
        parse_procedure,
        procedure_path,
    )
    from tests.support.claims_terminals import delivery_definition
    from tests.support.procedures import procedure_members

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    original = parse_procedure(
        tree[procedure_path(fixtures.PROCEDURE_NAME)], path=procedure_path(fixtures.PROCEDURE_NAME)
    )
    definition, contracts = delivery_definition(
        original,
        subject_kind=KIND,
        subject_id="click",
        predicates=(SEVERITY,),
        records="/proposals/*",
        create=True,
    )
    # The new Procedure is a separate governed declaration.
    definition = definition.model_copy(update={"name": "stable-import"})
    from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest

    updated = original.model_copy(
        update={
            "identity": ArtifactIdentity(kind="Procedure", name="stable-import"),
            "definition": definition,
            "definition_digest": compute_procedure_definition_digest(definition).tagged,
        }
    )
    new_line = fixtures._served_line(instance, updated, fixtures._policy()).model_copy(
        update={
            "identity": ArtifactIdentity(kind="Line", name="stable-import"),
            "max_authority": "propose",
        }
    )
    mandate = fixtures._line_mandate(new_line, updated).model_copy(
        update={
            "identity": ArtifactIdentity(kind="ProcedureMandate", name="stable-import-mandate"),
            "namespace": ("claims", "subjects"),
        }
    )
    from cruxible_client.contracts.procedures.line_specs import line_spec_path, render_line_spec
    from cruxible_client.contracts.shapes import contract_path, render_contract

    fixtures._accept_more(
        instance,
        owner,
        {
            **procedure_members(updated),
            **{contract_path(c.ref.name, c.ref.version): render_contract(c) for c in contracts},
            line_spec_path(new_line.identity.name): render_line_spec(new_line),
            procedure_mandate_path(mandate.identity.name): render_procedure_mandate(mandate),
        },
        name="stable-import",
    )
    (root / fixtures.RELATIVE_PATH).write_bytes(
        canonical_bytes(
            {
                "proposals": [
                    {"id": "click", "severity": "high"},
                    {"id": "flask", "severity": "high"},
                ]
            }
        )
    )
    egress = _delivered(instance, delivery.run_line(instance, root, new_line))
    accept_proposal(
        instance, owner, service_inspect_playbill_proposal(instance, proposal_id=egress.proposal_id)
    )
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    severity = sorted(
        parse_claim(tree[path], path=path).statement.subject.artifact_path
        for path in tree
        if path.startswith("claims/")
        and parse_claim(tree[path], path=path).statement.predicate == SEVERITY
        and parse_claim(tree[path], path=path).lifecycle.state == "live"
    )
    assert severity == [f"subjects/{KIND}/click.json", f"subjects/{KIND}/flask.json"]


def test_a_procedure_delivery_records_each_creations_actual_outcome(tmp_path: Path) -> None:
    """The operation's creation record holds the created first holder it assigned (F-001)."""

    from cruxible_core.authoring.assignment_records import SubjectAssignmentRecords
    from cruxible_core.indexes.subject_identity import RecordedCreation

    instance, _owner, root, line = subject_world(tmp_path, (subject_item(),))
    _delivered(instance, delivery.run_line(instance, root, line))
    records = SubjectAssignmentRecords(instance.root / instance.descriptor.storage.exhaust)
    entries = {
        entry
        for path in records.directory.glob("AIT-*.json")
        for record in (records.record(path.stem),)
        if record is not None
        for entry in record.recorded()
        if entry.kind == KIND
    }
    (entry,) = entries
    assert entry == RecordedCreation(
        member=entry.member,
        kind=KIND,
        key=entry.key,
        rule="first_holder",
        outcome="created",
        subject_id="click",
    )


# -- E-b delta-3 review F-001: a qualified alias is an alias in the stability check ----


def _qualified_alias(owner_slug: str, value: str) -> dict[str, object]:
    """A v3 item (owner named by key) giving ``owner_slug`` the qualified alias ``value``."""

    return {
        "tag": "playbill-procedure-claim-proposal-item-v3",
        "statement": {
            "tag": "playbill-procedure-claim-statement-v1",
            "subject": {"subject_kind": KIND, "key": {"slug": owner_slug}},
            "predicate": "semantic.alias",
            "qualifier": "local-name",
            "object": {"kind": "literal", "value": value},
            "role": "normative",
        },
        "rationale": f"Another name for {owner_slug}",
        "source": _self_source(f"also known as {value}"),
    }


def test_a_qualified_alias_cannot_redirect_a_sibling_key(tmp_path: Path) -> None:
    instance, owner, root = _identity_world(tmp_path, "click", "other name")
    line = _identity_terminal(
        instance,
        owner,
        root,
        [
            {"id": "click", "alias": "other name", "severity": "high"},
            {"id": "other name", "severity": "low"},
        ],
        mappings=[
            _mapping(
                "semantic.alias", "$item.fields.alias", role="normative", qualifier="local-name"
            ),
            _SEVERITY,
        ],
    )
    egress = _delivered(instance, delivery.run_line(instance, root, line))
    claims = _identity_claims(instance, egress)
    (alias,) = [c for c in claims if c.statement.predicate == "semantic.alias"]
    assert alias.statement.qualifier == "local-name"
    assert alias.statement.subject.artifact_path == f"subjects/{KIND}/click.json"
    (sibling,) = [
        c for c in claims if c.statement.predicate == SEVERITY and c.statement.object.value == "low"
    ]
    assert sibling.statement.subject.artifact_path == f"subjects/{KIND}/other-name.json"
    _identity_accept(instance, owner, egress)
    assert _holder(instance, "other name") == sibling.statement.subject.artifact_path


def test_a_qualified_alias_recovery_preserves_sibling_subjects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import timedelta

    from cruxible_core.service.procedures.procedure_runs import service_get_playbill_procedure_run
    from cruxible_core.service.proposals.proposal_egress import service_recover_proposal_egress

    instance, owner, root = _identity_world(tmp_path, "click", "other name")
    line = _identity_terminal(
        instance,
        owner,
        root,
        [
            {"id": "click", "alias": "other name", "severity": "high"},
            {"id": "other name", "severity": "low"},
        ],
        mappings=[
            _mapping(
                "semantic.alias", "$item.fields.alias", role="normative", qualifier="local-name"
            ),
            _SEVERITY,
        ],
    )
    base = instance.accepted_coordinate()
    refs = set(delivery._proposal_refs(instance))
    with monkeypatch.context() as crash:
        delivery._crash_after(crash, after_submit=True)
        with pytest.raises(delivery._Crash, match="^$"):
            delivery.run_line(instance, root, line)
    assert len(set(delivery._proposal_refs(instance)) - refs) == 1
    recovered = service_recover_proposal_egress(
        instance, recorded_at=fixtures.NOW + timedelta(minutes=1)
    )
    ((run_id, disposition),) = recovered.items()
    assert disposition == "delivered" and instance.accepted_coordinate() == base
    state = service_get_playbill_procedure_run(instance, run_id=run_id)
    (egress,) = state.terminal_egress
    claims = _identity_claims(instance, egress)
    (alias,) = [c for c in claims if c.statement.predicate == "semantic.alias"]
    assert alias.statement.qualifier == "local-name"
    assert alias.statement.subject.artifact_path == f"subjects/{KIND}/click.json"
    assert alias.statement.object.value == "other name"
    assert {
        c.statement.subject.artifact_path: c.statement.object.value
        for c in claims
        if c.statement.predicate == SEVERITY
    } == {f"subjects/{KIND}/click.json": "high", f"subjects/{KIND}/other-name.json": "low"}
    assert (
        service_recover_proposal_egress(instance, recorded_at=fixtures.NOW + timedelta(minutes=2))
        == {}
    )
    assert len(set(delivery._proposal_refs(instance)) - refs) == 1
    _identity_accept(instance, owner, egress)
    assert _holder(instance, "other name") == f"subjects/{KIND}/other-name.json"


def _identity_settle_grant(instance, owner, line):
    from cruxible_client.contracts.artifacts import ArtifactPin
    from cruxible_client.contracts.claim_types import claim_type_digest, parse_claim_type
    from cruxible_client.contracts.procedure_mandates import (
        MandateClaimScope,
        MandateCondition,
        procedure_mandate_digest,
    )
    from cruxible_client.contracts.procedures.line_specs import line_spec_digest
    from cruxible_client.contracts.query.definitions import (
        query_definition_digest,
        query_definition_path,
        render_query_definition,
    )
    from tests.support.mandates import procedure_mandate, scope_changes
    from tests.test_procedures.test_procedure_settlement_delivery import _condition

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    query = _condition(only_subject=None)
    query = query.model_copy(
        update={
            "entry": query.entry.model_copy(update={"subject_kinds": (KIND, "semantic.subject")})
        }
    )
    types = [
        parse_claim_type(tree[claim_type_path(p)], path=claim_type_path(p))
        for p in (SEVERITY, "semantic.alias")
    ]
    mandate = procedure_mandate(
        "identity-settle",
        grants="settle",
        line=ArtifactPin(
            role="line", target=line.identity, artifact_digest=line_spec_digest(line).tagged
        ),
        scope=tuple(
            MandateClaimScope(
                claim_type=ArtifactPin(
                    role="claim-type",
                    target=ct.identity,
                    artifact_digest=claim_type_digest(ct).tagged,
                ),
                changes=scope_changes("create", "revise"),
            )
            for ct in types
        ),
        condition=MandateCondition(
            query=ArtifactPin(
                role="condition-query",
                target=query.identity,
                artifact_digest=query_definition_digest(query).tagged,
            ),
            binding_parameter="advisory_id",
            required_fields=("id",),
            fallback="refuse",
        ),
    )
    fixtures._accept_more(
        instance,
        owner,
        {
            query_definition_path(query.identity.name): render_query_definition(query),
            procedure_mandate_path(mandate.identity.name): render_procedure_mandate(mandate),
        },
        name="identity-settle",
    )
    return {procedure_mandate_digest(mandate).tagged: mandate}


def test_propose_and_settle_preparation_pin_the_same_subjects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.procedures import proposal_delivery

    instance, owner, root = _identity_world(tmp_path, "click", "other name")
    line = _identity_terminal(
        instance,
        owner,
        root,
        [
            {"id": "click", "alias": "other name", "severity": "high"},
            {"id": "other name", "severity": "low"},
        ],
        mappings=[
            _mapping(
                "semantic.alias", "$item.fields.alias", role="normative", qualifier="local-name"
            ),
            _SEVERITY,
        ],
    )
    mandates = _identity_settle_grant(instance, owner, line)
    seen = []
    prepare = proposal_delivery.ProposalTerminalEgressSink.prepare_terminal_egress

    def recorded(self, **arguments):
        result = prepare(self, **arguments)
        seen.append((arguments, result))
        return result

    monkeypatch.setattr(
        proposal_delivery.ProposalTerminalEgressSink, "prepare_terminal_egress", recorded
    )
    egress = _delivered(instance, delivery.run_line(instance, root, line))
    ((arguments, proposed),) = seen
    request = arguments["request"]
    assert request.kind == "claims" and request.required_rung == 2
    settling = type(request).model_validate(
        {
            **request.model_dump(mode="python"),
            "effective_rung": 3,
            "required_rung": 3,
            "granted_operation": "activate_change_set",
            "capped_by": None,
        }
    )
    sink = proposal_delivery.ProposalTerminalEgressSink(
        instance=instance, accepted_mandates=mandates
    )
    prepared = prepare(sink, **{**arguments, "request": settling})
    assert prepared.procedure_mandate_digest in mandates
    assert prepared.lowering_digest == proposed.lowering_digest
    assert prepared.item_paths == proposed.item_paths
    assert prepared.target_paths == proposed.target_paths
    claims = _identity_claims(instance, egress)
    (alias,) = [c for c in claims if c.statement.predicate == "semantic.alias"]
    assert alias.statement.qualifier == "local-name"
    assert alias.statement.subject.artifact_path == f"subjects/{KIND}/click.json"
    assert alias.statement.object.value == "other name"
    assert {
        c.statement.subject.artifact_path: c.statement.object.value
        for c in claims
        if c.statement.predicate == SEVERITY
    } == {f"subjects/{KIND}/click.json": "high", f"subjects/{KIND}/other-name.json": "low"}


def test_retired_command_node_refuses_before_identity_lowering() -> None:
    from cruxible_client.contracts.procedures.authored import ProjectStep
    from tests.support.procedures import procedure_spec

    with pytest.raises(ValidationError, match="procedure_node_kind_retired.*typed claims step"):
        procedure_spec(
            procedure="retired-key-redirect",
            input={},
            output={},
            steps=[
                ProjectStep(project="rows", fields={}),
                {
                    "propose": (
                        _qualified_alias("click", "other name"),
                        keyed_claim_item("other name"),
                    )
                },
            ],
        )


def test_a_subject_item_citing_the_runs_capture_writes_a_supported_key_claim(
    tmp_path: Path,
) -> None:
    """The key ClaimType has no evidence rule and the mandate no self-source entry.

    The key Claim cites the run's own Capture (no ``source``), which no rule of the
    example-shaped key ClaimType admits: its Subject's creation covers it (Claim
    law v2 revision 9), so the proposal lands and the key Claim is supported.
    """

    from cruxible_client.contracts.claim_reads import ClaimValuesRequest
    from cruxible_core.service.claims.claim_reads import service_read_claim_values
    from tests.core_support._retirement_support import activate_proposal

    # Keyed by what the run read, so the item's closure reaches the Source Capture.
    item = subject_item("$steps.result.severity")
    del item["source"]
    instance, owner, root, line = subject_world(
        tmp_path, (item,), namespace=("claims", f"subjects/{KIND}")
    )

    egress = _delivered(instance, delivery.run_line(instance, root, line))
    assert not any(path.startswith("capture-contracts/") for path in egress.target_paths)
    activate_proposal(instance, owner, egress.proposal_id)

    (row,) = service_read_claim_values(
        instance,
        request=ClaimValuesRequest(
            subject_paths=(f"subjects/{KIND}/high.json",), predicates=(SLUG,)
        ),
    ).values
    assert row.verdict == "supported"


def test_a_reimport_naming_live_subjects_restates_their_claims(tmp_path: Path) -> None:
    """Re-importing a document whose Subjects already exist (the SBOM re-import):
    the terminal's own Subject item names the live Subject, so a Claim item on
    its key restates the live Claim -- unchanged content changes nothing, new
    content behind the same statement revises it -- and never refuses
    ``existing_claim_dispositions_incomplete`` on the one-valued slot."""

    from datetime import timedelta

    instance, owner, root, line = subject_world(
        tmp_path, (subject_item(), keyed_claim_item()), document=True
    )
    first = _delivered(instance, delivery.run_line(instance, root, line))
    accept_proposal(
        instance, owner, service_inspect_playbill_proposal(instance, proposal_id=first.proposal_id)
    )
    head = instance.accepted_coordinate()

    same = _delivered(
        instance, delivery.run_line(instance, root, line, at=fixtures.NOW + timedelta(hours=1))
    )
    assert same.proposal_id is None and same.target_paths == ()
    assert all(child.unchanged for child in same.children), same.children
    assert instance.accepted_coordinate() == head

    # The document changes elsewhere, the severity stays: the new content
    # revises the live Claim's lineage instead of contending with it.
    (root / fixtures.RELATIVE_PATH).write_bytes(
        fixtures.canonical_bytes({"advisory_id": "OSV-2026-0002", "severity": "low"})
    )
    revised = _delivered(
        instance, delivery.run_line(instance, root, line, at=fixtures.NOW + timedelta(hours=2))
    )
    assert revised.proposal_id is not None
    accept_proposal(
        instance,
        owner,
        service_inspect_playbill_proposal(instance, proposal_id=revised.proposal_id),
    )
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    severities = [
        parse_claim(tree[path], path=path)
        for path in tree
        if path.startswith("claims/")
        and parse_claim(tree[path], path=path).statement.predicate == SEVERITY
    ]
    live = [claim for claim in severities if claim.lifecycle.state == "live"]
    assert [claim.statement.object.model_dump(mode="json")["value"] for claim in live] == ["low"]
    assert live[0].statement.subject.artifact_path == f"subjects/{KIND}/click.json"
