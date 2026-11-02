"""A `propose_change_set` terminal creates Subjects by kind and key, never by id (identity D1).

A Subject item names a declared kind and its key values; core assigns the id. A
key a live Subject already holds names that Subject, and the item creates
nothing (reported ``reused``). A Claim item of the same terminal names a Subject
the run knows only by its key with a ``SubjectKeyReference`` (v3 item): it
resolves to the Subject the terminal's own Subject item lands on, else through
the common resolver over the base (exact live id, alias, key, exact retired
id), and refuses when none or several hold it. Every reference is checked again
over the final candidate; one that resolves otherwise there refuses the terminal
(``cruxible.procedure.identity_reference_unstable``).
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest

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
from tests.core_support._light_capture_support import record_contract as capture_contract
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
    # Native templates bind vocabulary when accepted, before a run can write.
    with pytest.raises(AssertionError, match="cruxible.change_set.unresolved_pin"):
        subject_world(
            tmp_path, (subject_item(subject_kind="security.vendor", key={"slug": "acme"}),)
        )


def test_create_new_on_a_unique_kind_refuses(tmp_path: Path) -> None:
    from pydantic import ValidationError

    from cruxible_client.contracts.procedures.claims import ClaimsSubject

    with pytest.raises(ValidationError) as refused:
        ClaimsSubject(kind=KIND, key={"slug": "click"}, create="create_new")
    assert refused.value.errors()[0]["type"] == "literal_error"
    assert refused.value.errors()[0]["loc"] == ("create",)


# -- E-b delta review F-002: one terminal's key changes are one overlay -------------------


def test_a_reference_that_a_key_move_and_recreation_would_redirect_refuses(tmp_path: Path) -> None:
    """Retired command items cannot redirect a reference or choose a predecessor."""
    from pydantic import ValidationError

    from cruxible_client.contracts.procedures.authored import ProjectStep
    from tests.support.procedures import procedure_spec

    command = _qualified_alias("click", "other name")
    command["revises"] = "CLM-arbitrary-predecessor"
    with pytest.raises(ValidationError, match="procedure_node_kind_retired.*typed claims step"):
        procedure_spec(
            procedure="retired-key-redirect",
            input={},
            output={},
            steps=[
                ProjectStep(project="rows", fields={}),
                {"propose": (command, keyed_claim_item("other name"))},
            ],
        )


def test_a_frozen_key_missing_the_terminal_would_supply_refuses_as_unstable(tmp_path: Path) -> None:
    """Retired command items cannot redirect a reference or choose a predecessor."""
    from pydantic import ValidationError

    from cruxible_client.contracts.procedures.authored import ProjectStep
    from tests.support.procedures import procedure_spec

    command = _qualified_alias("click", "other name")
    command["revises"] = "CLM-arbitrary-predecessor"
    with pytest.raises(ValidationError, match="procedure_node_kind_retired.*typed claims step"):
        procedure_spec(
            procedure="retired-key-redirect",
            input={},
            output={},
            steps=[
                ProjectStep(project="rows", fields={}),
                {"propose": (command, keyed_claim_item("other name"))},
            ],
        )


def test_a_keyed_alias_owner_that_redirects_a_sibling_reference_refuses(tmp_path: Path) -> None:
    """Retired command items cannot redirect a reference or choose a predecessor."""
    from pydantic import ValidationError

    from cruxible_client.contracts.procedures.authored import ProjectStep
    from tests.support.procedures import procedure_spec

    command = _qualified_alias("click", "other name")
    with pytest.raises(ValidationError, match="procedure_node_kind_retired.*typed claims step"):
        procedure_spec(
            procedure="retired-key-redirect",
            input={},
            output={},
            steps=[
                ProjectStep(project="rows", fields={}),
                {"propose": (command, keyed_claim_item("other name"))},
            ],
        )


def test_a_keyed_owner_moving_its_key_then_referenced_by_it_refuses(tmp_path: Path) -> None:
    """Retired command items cannot redirect a reference or choose a predecessor."""
    from pydantic import ValidationError

    from cruxible_client.contracts.procedures.authored import ProjectStep
    from tests.support.procedures import procedure_spec

    command = _qualified_alias("click", "other name")
    command["revises"] = "CLM-arbitrary-predecessor"
    with pytest.raises(ValidationError, match="procedure_node_kind_retired.*typed claims step"):
        procedure_spec(
            procedure="retired-key-redirect",
            input={},
            output={},
            steps=[
                ProjectStep(project="rows", fields={}),
                {"propose": (command, keyed_claim_item("other name"))},
            ],
        )


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


def test_a_qualified_alias_redirecting_a_sibling_reference_refuses(tmp_path: Path) -> None:
    """Retired command items cannot redirect a reference or choose a predecessor."""
    from pydantic import ValidationError

    from cruxible_client.contracts.procedures.authored import ProjectStep
    from tests.support.procedures import procedure_spec

    command = _qualified_alias("click", "other name")
    with pytest.raises(ValidationError, match="procedure_node_kind_retired.*typed claims step"):
        procedure_spec(
            procedure="retired-key-redirect",
            input={},
            output={},
            steps=[
                ProjectStep(project="rows", fields={}),
                {"propose": (command, keyed_claim_item("other name"))},
            ],
        )


def test_a_qualified_alias_terminal_refuses_before_its_door_so_recovery_has_nothing(
    tmp_path: Path,
) -> None:
    """Retired command items cannot redirect a reference or choose a predecessor."""
    from pydantic import ValidationError

    from cruxible_client.contracts.procedures.authored import ProjectStep
    from tests.support.procedures import procedure_spec

    command = _qualified_alias("click", "other name")
    with pytest.raises(ValidationError, match="procedure_node_kind_retired.*typed claims step"):
        procedure_spec(
            procedure="retired-key-redirect",
            input={},
            output={},
            steps=[
                ProjectStep(project="rows", fields={}),
                {"propose": (command, keyed_claim_item("other name"))},
            ],
        )


def test_settle_preparation_refuses_the_same_unstable_terminal(tmp_path: Path) -> None:
    """Retired command items cannot redirect a reference or choose a predecessor."""
    from pydantic import ValidationError

    from cruxible_client.contracts.procedures.authored import ProjectStep
    from tests.support.procedures import procedure_spec

    command = _qualified_alias("click", "other name")
    with pytest.raises(ValidationError, match="procedure_node_kind_retired.*typed claims step"):
        procedure_spec(
            procedure="retired-key-redirect",
            input={},
            output={},
            steps=[
                ProjectStep(project="rows", fields={}),
                {"propose": (command, keyed_claim_item("other name"))},
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
