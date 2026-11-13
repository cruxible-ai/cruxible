"""An accepted extract pins its row (ruling L1): a kind whose row grows does not change it.

The author writes the thin node; acceptance adds the row Contract pin of the
record set it names. Every later read takes that pinned row and only its fields,
with paths and rules from the kind's layout at the run's coordinate. So moving a
record set's row to a later version (grow-only: an added optional field) changes
neither the record envelope an accepted Procedure returns, nor its Claims, nor the
extract spec its receipts cite. The Procedure adopts the new row by being
accepted again. A stored node without the pin is refused by the Procedure law.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.captures import (
    capture_contract_digest,
    capture_contract_path,
    parse_capture_contract,
    render_capture_contract,
)
from cruxible_client.contracts.procedures.artifacts import (
    parse_procedure,
    procedure_path,
    render_procedure,
)
from cruxible_client.contracts.procedures.extract import extract_spec_digest
from cruxible_client.contracts.procedures.layout_extract import LayoutExtractNode
from cruxible_client.contracts.shapes import (
    Contract,
    contract_digest,
    contract_path,
    render_contract,
    validate_contract,
)
from cruxible_core.procedures.extract import extract_spec
from tests.core_support._candidate_support import submit_member_candidate
from tests.core_support._identity_support import accept_tree
from tests.support.procedures import procedure_limits, procedure_spec
from tests.test_procedures.test_claims_terminal import (
    ROW,
    activate,
    candidate_claims,
    claims_spec,
    claims_world,
    install,
    run,
)

KIND_NAME = "test.orders-v1"
DOCUMENT = {
    "entries": [
        {"id": "CVE-2026-0001", "status": "open", "note": "first", "extra": "x"},
        {"id": "CVE-2026-0002", "status": "closed", "note": "second", "extra": "y"},
    ]
}
#: ROW's successor: one optional field more (a grow-only row move).
ROW_V2 = validate_contract(
    {
        "name": ROW.name,
        "version": 2,
        "predecessor_digest": contract_digest(ROW).tagged,
        "fields": {
            "status": "enum[open, closed, unknown]",
            "note": "string{max_len: 256}?",
            "extra": "string{max_len: 64}?",
        },
    }
)


def _head(instance: Any) -> dict[str, bytes]:
    return instance.tree_at(instance.accepted_coordinate().git_oid)


def _kind_at(tree: dict[str, bytes]) -> Any:
    path = capture_contract_path(KIND_NAME)
    return parse_capture_contract(tree[path], path=path)


def _contract_at(tree: dict[str, bytes], ref: Any) -> Contract:
    from cruxible_client.contracts.shapes import parse_contract

    path = contract_path(ref.name, ref.version)
    return parse_contract(tree[path], path=path)


def _accepted_extract(tree: dict[str, bytes], name: str) -> LayoutExtractNode:
    path = procedure_path(name)
    (node,) = [
        n
        for n in parse_procedure(tree[path], path=path).definition.nodes
        if isinstance(n, LayoutExtractNode)
    ]
    return node


def _grow_entries_row(instance: Any) -> None:
    """Accept ROW@2 and a kind successor whose ``entries`` set reads it (adding ``extra``)."""

    accepted = _kind_at(_head(instance))
    layout = accepted.layout.model_dump(mode="json")
    entries = layout["records"]["entries"]
    entries["row"] = {
        "name": ROW_V2.name,
        "version": 2,
        "digest": contract_digest(ROW_V2).tagged,
    }
    entries["fields"]["extra"] = {"at": "extra"}
    successor = accepted.model_copy(
        update={
            "layout": type(accepted.layout).model_validate(layout),
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=capture_contract_digest(accepted).tagged
            ),
        }
    )
    accept_tree(
        instance,
        {
            contract_path(ROW_V2.name, 2): render_contract(ROW_V2),
            capture_contract_path(KIND_NAME): render_capture_contract(successor),
        },
        name="row-grows",
    )


def _resolved(instance_tree: dict[str, bytes], node: LayoutExtractNode) -> Any:
    kind = _kind_at(instance_tree)
    assert node.row is not None
    return extract_spec(node, kind, _contract_at(instance_tree, node.row))


def _records_spec(name: str = "test.records") -> Any:
    """read the file -> extract ``entries`` -> return its records, typed by ROW@1."""

    base = claims_spec().model_dump(mode="python", by_alias=True)
    call, extract = base["steps"][0], base["steps"][1]
    return procedure_spec(
        name,
        reference_text=True,
        input={},
        output={"items": f"list<record<{ROW.name}@1>>{{max_items: 100}}"},
        slots={"fetch": {"capability": "workspace.file@1"}},
        hard_caps=procedure_limits(provider_calls=1, capture_bytes=65536),
        steps=[call, extract],
        returns={"fields": {"items": "$steps.entries.records"}},
    )


def test_acceptance_pins_the_row_of_the_record_set_the_node_names(tmp_path: Path) -> None:
    instance, _root = claims_world(tmp_path, DOCUMENT)
    install(instance, claims_spec())
    node = _accepted_extract(_head(instance), "test.claims")
    assert node.row is not None
    assert (node.row.name, node.row.version) == (ROW.name, 1)
    assert node.row.digest == contract_digest(ROW).tagged


def test_a_grown_row_keeps_an_accepted_procedures_record_envelope(tmp_path: Path) -> None:
    """The review's input: a Procedure returning its records typed by ROW@1 keeps running."""

    instance, root = claims_world(tmp_path, DOCUMENT)
    install(instance, _records_spec(), line_name="test.records-line", authority="observe")
    first = run(instance, root, line="test.records-line")
    assert first.status == "succeeded", first.terminal
    first_tree = _head(instance)

    _grow_entries_row(instance)
    second = run(instance, root, minute=1, line="test.records-line")

    assert second.status == "succeeded", second.terminal
    assert second.result == first.result
    for item in second.result["items"]:
        assert set(item["fields"]) == {"status", "note"}
    node = _accepted_extract(_head(instance), "test.records")
    # The spec the receipts cite is unchanged across the kind successor.
    before, after = _resolved(first_tree, node), _resolved(_head(instance), node)
    assert before == after
    assert extract_spec_digest(before) == extract_spec_digest(after)


def test_a_grown_row_keeps_the_claims_and_their_receipts(tmp_path: Path) -> None:
    instance, root = claims_world(tmp_path, DOCUMENT)
    install(instance, claims_spec())
    first = run(instance, root)
    first_claims = candidate_claims(instance, first)
    receipts = {c.backing.procedure_evidence.value.extract_spec_digest for c in first_claims}
    first_tree = _head(instance)
    activate(instance, first)

    _grow_entries_row(instance)
    second = run(instance, root, minute=1)

    assert second.status == "succeeded", second.terminal
    assert second.terminal_egress[0].proposal_id is None
    assert all(child.unchanged for child in second.terminal_egress[0].children)
    node = _accepted_extract(_head(instance), "test.claims")
    # Replaying the first run at its own coordinate resolves the spec its receipts cite,
    # and so does the head after the kind grew.
    (receipt,) = receipts
    assert extract_spec_digest(_resolved(first_tree, node)) == receipt
    assert extract_spec_digest(_resolved(_head(instance), node)) == receipt


def test_a_re_accepted_procedure_reads_the_grown_row(tmp_path: Path) -> None:
    instance, root = claims_world(tmp_path, DOCUMENT)
    install(instance, _records_spec(), line_name="test.records-line", authority="observe")
    _grow_entries_row(instance)

    # The same spec authored again pins the record set's current row.
    grown = _records_spec().model_dump(mode="python", by_alias=True)
    grown["output"] = {"items": f"list<record<{ROW.name}@2>>{{max_items: 100}}"}
    from cruxible_client.contracts.authoring.inputs import (
        ChangeSetInput,
        LineInput,
        ProcedureInput,
        ProcedureMandateInput,
    )
    from cruxible_client.contracts.procedure_mandates import (
        parse_procedure_mandate,
        procedure_mandate_path,
    )
    from tests.support.operation_world import author

    revised = procedure_spec(base=grown, reference_text=True)
    mandate_path = procedure_mandate_path("test.records-line")
    mandate = parse_procedure_mandate(_head(instance)[mandate_path], path=mandate_path)
    # The Procedure, its Line and the exact grant are renewed in one closure.
    author(
        instance,
        ChangeSetInput(
            kind="change_set",
            members=(
                ProcedureInput(kind="procedure", spec=revised),
                LineInput(
                    kind="line",
                    name="test.records-line",
                    procedure_name=revised.procedure,
                    max_authority="observe",
                ),
                ProcedureMandateInput(
                    kind="procedure_mandate",
                    name=mandate.identity.name,
                    line_name="test.records-line",
                    grants="propose",
                    namespace=mandate.namespace,
                    valid_from=mandate.valid_from,
                    expires_at=mandate.expires_at,
                ),
            ),
        ),
        name="re-accept",
    )
    node = _accepted_extract(_head(instance), "test.records")
    assert node.row is not None and node.row.version == 2
    assert "extra" in _resolved(_head(instance), node).fields
    state = run(instance, root, minute=1, line="test.records-line")
    assert state.status == "succeeded", state.terminal
    assert [item["fields"]["extra"]["value"] for item in state.result["items"]] == ["x", "y"]


def test_a_stored_node_without_its_row_pin_is_refused(tmp_path: Path) -> None:
    from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest

    instance, _root = claims_world(tmp_path, DOCUMENT)
    install(instance, claims_spec())
    from cruxible_client.contracts.artifacts import ArtifactIdentity

    accepted = parse_procedure(
        _head(instance)[procedure_path("test.claims")], path=procedure_path("test.claims")
    )
    # A new Procedure (no dependents to renew) whose extract states no row pin.
    path = procedure_path("test.unpinned")
    nodes = tuple(
        n.model_copy(update={"row": None}) if isinstance(n, LayoutExtractNode) else n
        for n in accepted.definition.nodes
    )
    definition = accepted.definition.model_copy(update={"nodes": nodes, "name": "test.unpinned"})
    unpinned = accepted.model_copy(
        update={
            "identity": ArtifactIdentity(kind="Procedure", name="test.unpinned"),
            "definition": definition,
            "definition_digest": compute_procedure_definition_digest(definition).tagged,
            "lifecycle": ArtifactLifecycle(),
        }
    )
    inspection = submit_member_candidate(
        instance,
        members={path: render_procedure(unpinned)},
        actor_id="owner",
        proposal_name="unpinned-extract",
        proposal_family="procedure",
        timestamp="2026-11-12T12:00:00.000000Z",
    )
    assert inspection.proposal.candidate is None
    codes = [item.code for item in inspection.proposal.evaluation.diagnostics]
    assert codes == ["cruxible.procedure.extract_row_unpinned"], codes
