"""D-R14–16: new extraction lineage, safe fallbacks and retired findings."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.authoring.inputs import (
    ChangeSetInput,
    LineInput,
    ProcedureInput,
    ProcedureMandateInput,
)
from cruxible_client.contracts.captures import observed_content, parse_capture_envelope
from cruxible_client.contracts.claim_evidence import ClaimEvidenceReceipt
from cruxible_client.contracts.claims import claim_artifact_digest
from cruxible_client.contracts.procedure_mandates import (
    parse_procedure_mandate,
    procedure_mandate_path,
)
from cruxible_client.contracts.procedures.line_specs import LineFindings
from cruxible_core.claims.claim_type_inputs import (
    ClaimTypeInputRecord,
    ClaimTypeInputValidationError,
    lower_claim_type_input,
)
from cruxible_core.evidence.claim_reobservations import (
    ClaimReobservationStore,
    ReobservationKey,
    ReobservationReader,
)
from cruxible_core.exhaust.records import parse_journal_payload
from cruxible_core.storage.cas import BodyAccessContext
from tests.support.operation_world import author, author_line, author_procedure
from tests.support.procedures import procedure_spec
from tests.test_procedures.test_claims_terminal import (
    PREDICATE,
    activate,
    candidate_claims,
    claim_type,
    claims_spec,
    claims_world,
    install,
    run,
)


@pytest.mark.parametrize("changed_value", [False, True])
def test_changed_extract_spec_reobserves_equal_values_and_revises_only_changed_values(
    tmp_path: Path, changed_value: bool
) -> None:
    from tests.support.layouts import record_set
    from tests.test_procedures.test_claims_terminal import KIND, ROW

    # The revised Procedure reads the same typed status from the note field: another
    # record set over the same path.
    from_note = record_set(
        at="/entries/*",
        row=ROW.ref.spelling,
        key={"cve_id": {"at": "id", "type": f"key_part<{KIND}:cve_id>"}},
        fields={"status": {"at": "note"}, "note": {"at": "note"}},
    )
    instance, root = claims_world(
        tmp_path,
        {
            "entries": [
                {"id": "CVE-2026-0001", "status": "open", "note": "open"},
                {
                    "id": "CVE-2026-0002",
                    "status": "open",
                    "note": "closed" if changed_value else "open",
                },
            ]
        },
        records={"entries_from_note": from_note},
    )
    spec = claims_spec()
    install(instance, spec)
    first = run(instance, root)
    original_claims = candidate_claims(instance, first)
    activate(instance, first)
    # A new Procedure reads the same typed status from a different source field.
    base = spec.model_dump(mode="json", by_alias=True)
    base["steps"][1]["records"] = "entries_from_note"
    revised = procedure_spec(base=base)
    mandate_path = procedure_mandate_path("test.claims-line")
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    mandate = parse_procedure_mandate(tree[mandate_path], path=mandate_path)
    # Procedure, dependent Line and exact grant are renewed in one governed closure.
    author(
        instance,
        ChangeSetInput(
            kind="change_set",
            members=(
                ProcedureInput(kind="procedure", spec=revised),
                LineInput(
                    kind="line",
                    name="test.claims-line",
                    procedure_name=revised.procedure,
                    max_authority="propose",
                ),
                ProcedureMandateInput(
                    kind="procedure_mandate",
                    name=mandate.identity.name,
                    line_name="test.claims-line",
                    grants="propose",
                    namespace=mandate.namespace,
                    valid_from=mandate.valid_from,
                    expires_at=mandate.expires_at,
                ),
            ),
        ),
        name="renew-extraction",
    )
    before = instance.accepted_coordinate()
    again = run(instance, root, minute=1)
    assert again.status == "succeeded", again.terminal
    assert instance.accepted_coordinate() == before
    if changed_value:
        (changed,) = candidate_claims(instance, again)
        assert changed.statement.object.value == "closed"
        predecessor = next(
            c for c in original_claims if c.statement.subject == changed.statement.subject
        )
        assert changed.identity == predecessor.identity
        assert changed.lifecycle.predecessor_digest == claim_artifact_digest(predecessor).tagged
    else:
        assert again.terminal_egress[0].proposal_id is None
        assert not again.terminal_egress[0].target_paths
    store = ClaimReobservationStore(instance)
    assert store.head_sequence() == (1 if changed_value else 2)
    journal = store._journal(create=False)
    assert journal is not None
    receipts = []
    for stored in journal.select_records(store.stream, partition_id="reobservations"):
        payload = parse_journal_payload(
            instance.body_store().read(
                stored.record.payload_digest,
                access=BodyAccessContext(principal_id="test", can_read_body=True),
            )
        )
        receipt = ClaimEvidenceReceipt.model_validate(payload["extraction"])
        assert (
            receipt.procedure_digest
            != original_claims[0].backing.procedure_evidence.procedure_digest
        )
        assert (
            receipt.value.extract_spec_digest
            != original_claims[0].backing.procedure_evidence.value.extract_spec_digest
        )
        claim = next(c for c in original_claims if c.identity.name == payload["claim_id"])
        original = parse_capture_envelope(
            instance.body_store().read(
                claim.backing.procedure_evidence.value.capture_digest,
                access=BodyAccessContext(principal_id="test", can_read_body=True),
            )
        )
        (observation,) = store.history(claim.identity.name)
        reading = ReobservationReader.of(instance).latest(
            claim.identity.name,
            (
                ReobservationKey(
                    claim.backing.procedure_evidence.value.capture_digest,
                    original.capture_contract_digest,
                    observed_content(original).digest,
                ),
            ),
            observation.recorded_at,
        )
        assert reading.evidence[0].observed_at > original.observed_at
        assert reading.evidence[0].reobserving_capture_digest == receipt.value.capture_digest
        receipts.append(receipt)
    assert len(receipts) == store.head_sequence()
    # Rebuilding the audit projection preserves the new receipt in its journal.
    store.path.unlink()
    assert sum(len(store.history(c.identity.name)) for c in original_claims) == len(receipts)
    tree = instance.tree_at(before.git_oid)
    from cruxible_client.contracts.claims import claim_path, parse_claim

    for claim in original_claims:
        path = claim_path(claim.identity.name)
        held = parse_claim(tree[path], path=path)
        assert held.backing.procedure_evidence == claim.backing.procedure_evidence


@pytest.mark.parametrize(
    "value,members,fallback,code,repair",
    [
        ("bool", (False,), True, "bool_fallback_refused", "remove it"),
        (
            "enum[open, closed, unknown]",
            ("closed",),
            "closed",
            "fallback_requires_complete_input",
            "choose a member outside",
        ),
    ],
)
def test_claim_type_acceptance_refuses_unsafe_fallbacks(value, members, fallback, code, repair):
    original = claim_type(value=value)
    raw = {
        key: value
        for key, value in original.model_dump(mode="json").items()
        if key in ClaimTypeInputRecord.model_fields
    }
    raw.update(requires_complete_input=members, incomplete_fallback=fallback)
    declared = ClaimTypeInputRecord.model_validate(raw)
    with pytest.raises(ClaimTypeInputValidationError, match=code) as error:
        lower_claim_type_input(declared, tree={})
    assert repair in str(error.value)
    assert error.value.error_code == "cruxible.claim_type.input_invalid"


def test_incomplete_boolean_false_is_skipped_and_reported(tmp_path: Path) -> None:
    instance, root = claims_world(
        tmp_path,
        {"entries": [{"id": "CVE-2026-0001", "status": "open"}]},
        types=(claim_type(value="bool", requires_complete_input=(False,)),),
    )
    base = claims_spec(mappings=[{"type": PREDICATE, "value": False}]).model_dump(
        mode="json", by_alias=True
    )
    base["slots"]["fetch"]["may_truncate"] = True
    install(instance, procedure_spec(base=base), findings=("incomplete_source",))
    before = instance.accepted_coordinate()
    state = run(instance, root)
    assert state.status == "succeeded", state.terminal
    assert instance.accepted_coordinate() == before
    assert state.terminal_egress[0].proposal_id is None
    assert not state.terminal_egress[0].target_paths
    (finding,) = state.findings
    assert finding.kind == "incomplete_source" and finding.input == "entries"
    from tests.test_procedures.test_claims_fix import _inbox

    assert any(item.payload["kind"] == "incomplete_source" for item in _inbox(instance))


def test_line_authoring_refuses_retired_precedence_finding(tmp_path: Path) -> None:
    instance, _ = claims_world(tmp_path, {"entries": []})
    spec = claims_spec()
    author_procedure(instance, spec)
    before = instance.accepted_coordinate()
    with pytest.raises(ValidationError, match="finding_kind_retired.*held_disagrees"):
        author_line(instance, procedure=spec.procedure, findings={"inbox": ["held_by_precedence"]})
    assert instance.accepted_coordinate() == before
    with pytest.raises(ValidationError, match="finding_kind_retired.*held_disagrees"):
        LineFindings(inbox=("held_by_precedence",))
    assert LineFindings(inbox=("held_disagrees",)).inbox == ("held_disagrees",)
