"""An absence proof cannot be filtered, forged, or inferred from a skipped write."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cruxible_client.contracts.claim_types import ClaimInverse
from cruxible_client.contracts.policies import ClaimResolutionPolicy
from tests.support.procedures import boundary_contract, procedure_spec
from tests.test_pilot.support import _watched
from tests.test_procedures.test_claims_absence import proposed_claims
from tests.test_procedures.test_claims_terminal import (
    KIND,
    PREDICATE,
    activate,
    claim_type,
    claims_spec,
    claims_world,
    install,
    run,
)

# Executor-only fixtures: these tests author Procedure step kinds the template
# closes at authoring (D-R37 h1) to cover their executor handlers (h3).
pytestmark = pytest.mark.executor_only


def safety_world(
    tmp_path,
    policy="mirror",
    *,
    subject_value=False,
    negative=False,
    object_key=False,
    numeric=False,
):
    value_type = (
        "int"
        if numeric
        else f"subject<{KIND}>"
        if subject_value
        else "enum[open, closed, unknown]"
        if negative
        else "string{max_len: 256}"
    )
    inverse = ClaimInverse(
        key={"kind": "object_key", "predicate": KIND + ".cve_id"}
        if object_key
        else {"kind": "object"},
        evidence_rules=("fixture",),
    )
    ct = claim_type(
        PREDICATE,
        value_type,
        cardinality="many",
        inverse=inverse,
        requires_complete_input=("open",) if negative else (),
        resolution_policy=ClaimResolutionPolicy(
            cardinality="many", eligible_verdicts=("supported",), selector="all"
        ),
    )
    row = boundary_contract(
        "test.claims-absence-safety.negative-row"
        if negative
        else "test.claims-absence-safety.text-row",
        {
            "status": "enum[open, closed, unknown]" if negative else "string{max_len: 256}",
            "note": "datetime?",
        },
    )
    document = {
        "entries": [
            {"id": "CVE-2026-0001", "status": "open", "note": "2026-10-09T12:00:00Z"},
            {"id": "CVE-2026-0002", "status": "open", "note": "2026-10-09T12:00:00Z"},
        ],
        "count": 2,
    }
    if numeric:
        for entry in document["entries"]:
            entry["status"] = "79"
    instance, root = claims_world(tmp_path, document, types=(ct,), rows=row)
    _watched(instance)
    value = (
        79
        if numeric
        else {"kind": KIND, "key": {"cve_id": "CVE-2026-0001"}}
        if subject_value
        else "open"
        if policy == "inverse"
        else "$item.fields.status"
    )
    raw = claims_spec(rows=row, mappings=[{"type": PREDICATE, "value": value}]).model_dump(
        mode="python", by_alias=True
    )
    raw["steps"][-1]["claims"][0]["when_absent"] = policy
    raw["steps"][1]["records"] = "entries_counted"
    raw["steps"].insert(1, {"read": "watched", "query": "security.watched-vulnerabilities"})
    raw["steps"][-1].update(expect="$steps.watched.rows")
    install(instance, procedure_spec(base=raw, reference_text=True))
    first = run(instance, root)
    assert first.status == "succeeded", first.terminal
    activate(instance, first)
    return instance, root, raw


def reinstall(instance, raw, name="safety-followup"):
    raw["procedure"] = "test." + name
    install(instance, procedure_spec(base=raw, reference_text=True), line_name=name)


@pytest.mark.parametrize("policy", ("mirror", "inverse"))
@pytest.mark.parametrize("skip", ("within", "for_each", "unresolved_subject"))
def test_enumerated_presence_survives_a_skipped_write(tmp_path: Path, policy, skip):
    instance, root, raw = safety_world(tmp_path, policy, subject_value=skip == "unresolved_subject")
    if skip == "within":
        raw["steps"].insert(
            -1,
            {
                "transform": "ineligible",
                "kind": "filter",
                "spec": {
                    "input": "$steps.watched.rows",
                    "where": {"op": "eq", "left": {"literal": 0}, "right": {"literal": 1}},
                },
            },
        )
        raw["steps"][-1]["subject"]["within"] = {
            "$ref": "step",
            "step": "ineligible",
            "path": ["rows"],
        }
    elif skip == "for_each":
        raw["steps"].insert(
            -1,
            {
                "transform": "selected",
                "kind": "filter",
                "spec": {
                    "input": "$steps.entries.records",
                    "where": {"op": "eq", "left": {"literal": 0}, "right": {"literal": 1}},
                },
            },
        )
        raw["steps"][-1]["for_each"] = "$steps.selected.records"
    else:
        raw["steps"][-1]["claims"][0]["value"] = {"kind": KIND, "key": {"cve_id": "CVE-2099-9999"}}
    reinstall(instance, raw)
    before = instance.accepted_coordinate()
    second = run(instance, root, minute=1, line="safety-followup")
    assert second.status == "succeeded", second.terminal
    assert not proposed_claims(instance, second)
    assert not any(f.kind == "claim_not_in_source" for f in second.findings)
    assert instance.accepted_coordinate() == before
    if skip == "within":
        assert second.completeness is not None
        assert second.completeness.expect_outside_within == 2


@pytest.mark.parametrize("skip", ("inadmissible", "line_break", "metadata", "per_value_incomplete"))
def test_unusable_present_values_never_authorize_absence(tmp_path: Path, monkeypatch, skip):
    instance, root, raw = safety_world(tmp_path, negative=skip == "per_value_incomplete")
    if skip == "metadata":
        raw["steps"][-1]["claims"][0]["effective_from"] = "$item.fields.note"
        reinstall(instance, raw)
    path = root / "data/doc.json"
    document = json.loads(path.read_bytes())
    if skip in {"inadmissible", "line_break"}:
        for row in document["entries"]:
            row["status"] = "x" * 300 if skip == "inadmissible" else "open\nline"
    elif skip == "metadata":
        for row in document["entries"]:
            row["note"] = "invalid-date"
    else:
        from cruxible_core.procedures.completeness import Completeness

        original = Completeness.at_sink

        def incomplete(self, reads):
            reads = tuple(reads)
            return False if any(alias == "entries" for alias, _ in reads) else original(self, reads)

        monkeypatch.setattr(Completeness, "at_sink", incomplete)
    path.write_text(json.dumps(document))
    second = run(
        instance,
        root,
        minute=1,
        line="safety-followup" if skip == "metadata" else "test.claims-line",
    )
    assert second.status == "succeeded", second.terminal
    assert not proposed_claims(instance, second)
    assert not any(f.kind == "claim_not_in_source" for f in second.findings)


@pytest.mark.parametrize(
    "invalid",
    (
        "transformed",
        "provider_records",
        "item_inverse",
    ),
)
def test_absence_authoring_rails_fail_with_typed_codes(tmp_path: Path, invalid):
    from cruxible_core.authoring.lowering import AuthoringLoweringError

    instance, root, raw = safety_world(tmp_path)
    raw["procedure"] = "test.refused-absence"
    expected = "claims_enumeration_invalid"
    if invalid == "transformed":
        # The root of for_each's chain is a query read, not an extract.
        raw["steps"][-1]["for_each"] = "$steps.watched.rows"
    elif invalid == "provider_records":
        # The read's value is the base kit's result: its one Capture field.
        raw["steps"][-1]["for_each"] = "$steps.doc.capture"
        expected = "claims_expect_provider_enumeration_unsupported"
    else:
        raw["steps"][-1]["claims"][0]["when_absent"] = "inverse"
        expected = "claims_inverse_item_value"
    with pytest.raises(AuthoringLoweringError) as caught:
        install(
            instance, procedure_spec(base=raw, reference_text=True), line_name="refused-absence"
        )
    assert expected in caught.value.code


@pytest.mark.parametrize("policy", ("mirror", "inverse"))
def test_another_logical_sources_positive_is_surfaced(tmp_path: Path, policy):
    instance, root, raw = safety_world(tmp_path, policy)
    raw["steps"][0]["input"]["logical_source"] = "another.logical.source"
    reinstall(instance, raw)
    (root / "data/doc.json").write_text(json.dumps({"entries": [], "count": 0}))
    second = run(instance, root, minute=1, line="safety-followup")
    assert second.status == "succeeded", second.terminal
    assert not proposed_claims(instance, second)
    assert len([f for f in second.findings if f.kind == "claim_not_in_source"]) == 2


@pytest.mark.parametrize("policy", ("mirror", "inverse"))
def test_a_witness_from_another_run_is_refused_at_delivery(tmp_path: Path, monkeypatch, policy):
    from cruxible_core.procedures.claims_changes import CoreRetirementChange
    from cruxible_core.procedures.proposal_delivery import ProposalTerminalEgressSink
    from tests.test_procedures.test_procedure_settlement_delivery import _egress_records

    instance, root, raw = safety_world(tmp_path, policy)
    (root / "data/doc.json").write_text(json.dumps({"entries": [], "count": 0}))
    first = run(instance, root, minute=1)
    assert first.status == "succeeded", first.terminal
    payload = next(p for p in _egress_records(instance, first) if p.get("verdict") == "prepared")
    from cruxible_core.procedures.egress import TerminalEgressRequestV2

    old_request = TerminalEgressRequestV2.model_validate(payload["request"])
    foreign = next(r.absence for r in old_request.claims_evidence.values() if r.absence is not None)
    reinstall(instance, raw)
    before = instance.accepted_coordinate()
    deliver = ProposalTerminalEgressSink.deliver_terminal_egress
    attempts = []

    def moved_witness(self, *, request, admission=None):
        assert request.run_id != foreign.run_id
        receipts = dict(request.claims_evidence)
        changes = dict(request.claims_changes)
        for key, receipt in receipts.items():
            if receipt.absence is None:
                continue
            receipts[key] = receipt.model_copy(update={"absence": foreign})
            if isinstance(changes[key], CoreRetirementChange):
                changes[key] = changes[key].model_copy(update={"witness": foreign})
        attempts.append(request.run_id)
        moved = request.model_copy(update={"claims_evidence": receipts, "claims_changes": changes})
        return deliver(self, request=moved, admission=admission)

    monkeypatch.setattr(ProposalTerminalEgressSink, "deliver_terminal_egress", moved_witness)
    second = run(instance, root, minute=2, line="safety-followup")
    assert attempts == [second.run_id]
    assert second.status == "node_refused", second.terminal
    assert second.terminal.code == "claims_absence_binding_invalid"
    assert not proposed_claims(instance, second)
    assert instance.accepted_coordinate() == before


@pytest.mark.parametrize("kind", ("retirement", "inverse"))
def test_public_authoring_cannot_mint_an_absence_witness(tmp_path: Path, kind):
    from cruxible_client.contracts.authoring.models import (
        ChangeSetAuthoringPayload,
        ClaimRetirementMember,
    )
    from cruxible_core.proposals.proposals import AuthenticatedActor
    from tests.test_authoring.test_authoring_change_set_intents import _coordinator

    instance, root, raw = safety_world(tmp_path, "mirror" if kind == "retirement" else "inverse")
    (root / "data/doc.json").write_text(json.dumps({"entries": [], "count": 0}))
    state = run(instance, root, minute=1)
    retired = proposed_claims(instance, state)
    if kind == "retirement":
        retired = [c for c in retired if c.retirement is not None]
        assert retired
        member = ClaimRetirementMember(
            retires=retired[0].identity.name,
            reason="superseded",
            absence=retired[0].retirement.absence,
        )
        expected = "cruxible.authoring.absence_requires_execution"
    else:
        from cruxible_client.contracts.authoring.models import (
            AuthoringClaimStatement,
            ClaimAuthoringPayload,
            ExistingCaptureCitationSource,
        )

        claim = next(c for c in retired if c.statement.object.kind == "none")
        member = ClaimAuthoringPayload(
            statement=AuthoringClaimStatement(
                subject=claim.statement.subject,
                predicate=claim.statement.predicate,
                object=claim.statement.object.model_dump(mode="json"),
                role=claim.statement.role,
            ),
            source=ExistingCaptureCitationSource(
                capture_digest=claim.backing.procedure_evidence.absence.capture
            ),
            procedure_evidence=claim.backing.procedure_evidence,
            citation_role="evidence",
            rationale="Forged public inverse.",
        )
        expected = "cruxible.authoring.derivation_requires_execution"
    coordinator = _coordinator(instance)
    created = coordinator.create(
        actor=AuthenticatedActor(actor_id="owner"),
        payload=ChangeSetAuthoringPayload(members=(member,), rationale="Forged public witness."),
        canonical_timestamp="2026-10-09T12:03:00.000000Z",
    )
    result = coordinator.preflight(
        created.intent.intent_id, actor=AuthenticatedActor(actor_id="owner")
    )
    assert result.verdict == "refused"
    assert any(d.code == expected for d in result.frontier.diagnostics)


def test_unresolved_held_unit_is_a_typed_run_refusal(tmp_path: Path, monkeypatch):
    from cruxible_core.procedures.claims_context import AcceptedClaimsContext

    instance, root, _ = safety_world(tmp_path, subject_value=True, object_key=True)
    original = AcceptedClaimsContext.slot

    def unresolved(self, subject, predicate, qualifier):
        return (
            () if predicate == KIND + ".cve_id" else original(self, subject, predicate, qualifier)
        )

    monkeypatch.setattr(AcceptedClaimsContext, "slot", unresolved)
    (root / "data/doc.json").write_text(json.dumps({"entries": [], "count": 0}))
    result = run(instance, root, minute=1)
    assert result.status == "node_refused"
    assert result.terminal.code == "claims_inverse_unit_unresolved"


@pytest.mark.parametrize("policy", ("mirror", "inverse"))
def test_mirror_and_inverse_need_no_count_declaration(tmp_path: Path, policy):
    """D-R19: declaring mirror/inverse is the completeness statement; no count required."""
    instance, root, raw = safety_world(tmp_path, policy)
    raw["steps"][2]["records"] = "entries"
    reinstall(instance, raw)
    (root / "data/doc.json").write_text(json.dumps({"entries": []}))
    result = run(instance, root, minute=1, line="safety-followup")
    assert result.status == "succeeded", result.terminal
    assert result.completeness is None
    changed = proposed_claims(instance, result)
    if policy == "mirror":
        # D-R24: mirror only retires, though this ClaimType declares an inverse.
        assert len(changed) == 2 and all(c.lifecycle.state == "retired" for c in changed)
        witnesses = [c.retirement.absence for c in changed]
    else:
        assert len(changed) == 2 and all(c.statement.object.kind == "none" for c in changed)
        witnesses = [c.backing.procedure_evidence.absence for c in changed]
    assert all(w is not None and w.complete_when is None for w in witnesses)
    assert not any(f.kind == "claim_not_in_source" for f in result.findings)


@pytest.mark.parametrize("policy", ("mirror", "inverse"))
def test_absence_obeys_within_and_counts_expected_subjects_outside_it(tmp_path: Path, policy):
    """D-R22/D-R24: one write fence; an expected Subject outside within is only counted."""
    instance, root, raw = safety_world(tmp_path, policy)
    raw["steps"].insert(
        -1,
        {
            "transform": "eligible",
            "kind": "filter",
            "spec": {
                "input": "$steps.watched.rows",
                "where": {
                    "op": "eq",
                    "left": {"$ref": "item", "path": ["fields", "cve_id"]},
                    "right": {"literal": "CVE-2026-0001"},
                },
            },
        },
    )
    raw["steps"][-1]["subject"]["within"] = {"$ref": "step", "step": "eligible", "path": ["rows"]}
    reinstall(instance, raw)
    (root / "data/doc.json").write_text(json.dumps({"entries": [], "count": 0}))
    result = run(instance, root, minute=1, line="safety-followup")
    assert result.status == "succeeded", result.terminal
    changed = proposed_claims(instance, result)
    assert len(changed) == 1
    assert {c.statement.subject.artifact_path for c in changed} == {
        f"subjects/{KIND}/cve-2026-0001.json"
    }
    if policy == "mirror":
        assert changed[0].lifecycle.state == "retired"
    else:
        assert changed[0].statement.object.kind == "none"
    assert not any(f.kind == "claim_not_in_source" for f in result.findings)
    assert result.completeness is not None
    assert result.completeness.complete and result.completeness.expect_outside_within == 1


def test_surface_without_a_count_declaration_keeps_existing_completeness(tmp_path: Path):
    instance, root, raw = safety_world(tmp_path, "surface")
    raw["steps"][2]["records"] = "entries"
    reinstall(instance, raw)
    (root / "data/doc.json").write_text(json.dumps({"entries": [], "count": 0}))
    result = run(instance, root, minute=1, line="safety-followup")
    assert result.status == "succeeded", result.terminal
    findings = [f for f in result.findings if f.kind == "claim_not_in_source"]
    assert len(findings) == 2 and not any(f.source_incomplete for f in findings)
    assert result.completeness is None
    assert not proposed_claims(instance, result)


def shaped_mirror(raw, *, rename=False):
    field = "renamed" if rename else "status"
    raw["steps"].insert(
        -1,
        {
            "transform": "typed",
            "kind": "shape",
            "spec": {
                "input": "$steps.entries.records",
                "fields": {field: {"from": "$item.fields.status", "as": "int"}},
            },
        },
    )
    raw["steps"][-1]["for_each"] = "$steps.typed.records"
    raw["steps"][-1]["claims"][0]["value"] = "$item.fields." + field


@pytest.mark.parametrize("rename", (False, True), ids=("cast", "renamed-cast"))
def test_covered_shape_cast_is_refused_at_authoring(tmp_path: Path, rename):
    from cruxible_core.authoring.lowering import AuthoringLoweringError

    instance, _root, raw = safety_world(tmp_path, numeric=True)
    shaped_mirror(raw, rename=rename)
    before = instance.accepted_coordinate()
    with pytest.raises(AuthoringLoweringError) as caught:
        reinstall(instance, raw)
    assert caught.value.code == "cruxible.authoring.claims_enumeration_invalid"
    assert "for_each" in str(caught.value)
    assert instance.accepted_coordinate() == before


@pytest.mark.parametrize("rename", (False, True), ids=("cast", "renamed-cast"))
def test_covered_shape_cast_is_refused_at_runtime(tmp_path: Path, monkeypatch, rename):
    from cruxible_core.procedures import claims_typing

    instance, root, raw = safety_world(tmp_path, numeric=True)
    shaped_mirror(raw, rename=rename)
    # Simulate a Procedure accepted before this rail: runtime must independently
    # refuse it, before a renamed template raises or a cast causes a retirement.
    with monkeypatch.context() as old_acceptance:
        old_acceptance.setattr(
            claims_typing, "absence_enumeration", lambda _node, aliases: aliases["entries"]
        )
        reinstall(instance, raw)
    before = instance.accepted_coordinate()
    result = run(instance, root, minute=1, line="safety-followup")
    assert result.status == "node_refused", result.terminal
    assert result.terminal.code == "claims_enumeration_invalid"
    assert "for_each" in result.terminal.message
    assert not proposed_claims(instance, result)
    assert instance.accepted_coordinate() == before


def test_covered_filter_dedupe_chain_preserves_presence(tmp_path: Path):
    instance, root, raw = safety_world(tmp_path)
    raw["steps"].insert(
        -1,
        {
            "transform": "unique",
            "kind": "dedupe",
            "spec": {"input": "$steps.entries.records", "keys": ["status"]},
        },
    )
    raw["steps"].insert(
        -1,
        {
            "transform": "selected",
            "kind": "filter",
            "spec": {
                "input": "$steps.unique.records",
                "where": {"op": "eq", "left": {"literal": 0}, "right": {"literal": 1}},
            },
        },
    )
    raw["steps"][-1]["for_each"] = "$steps.selected.records"
    reinstall(instance, raw)
    before = instance.accepted_coordinate()
    result = run(instance, root, minute=1, line="safety-followup")
    assert result.status == "succeeded", result.terminal
    assert not proposed_claims(instance, result)
    assert not any(f.kind == "claim_not_in_source" for f in result.findings)
    assert instance.accepted_coordinate() == before
